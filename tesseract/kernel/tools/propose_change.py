"""propose_change — generic operator-gated workspace mutation.

The assistant uses this to request a change to any operator-owned workspace `.md`
file (SOUL, USER, OPERATING, WORKSHOP, DIARY). The tool **never** writes the target
file — it appends a `change_proposal` event to the workspace inbox with
the full proposed content, an `expected_hash_before` snapshot, and a
unified diff. The operator approves or rejects in the workspace; on
Approve the `post_decision` REST handler performs the commit through
`apply_change()`.

Posture: `auto`. There is no chat ASK gate — the workspace event IS the
operator gate. (See `tesseract/kernel/workspace_changes.py` for the
allowlist + commit primitive.)
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.workspace_changes import (
    PROPOSABLE_PATHS,
    ProposeError,
    compute_diff,
    hash_text,
    preview_change,
    validate_action,
    validate_target,
    workspace_events_dir,
)
from tesseract.workspace_events.events import EventStore, WorkspaceEvent

logger = logging.getLogger(__name__)

_MAX_CONTENT_BYTES = 8 * 1024
_MAX_SUMMARY_CHARS = 240


class ProposeChangeInput(BaseModel):
    target_path: str = Field(
        description=(
            "Repo-relative path of the workspace file to change. Allowed: "
            + ", ".join(sorted(PROPOSABLE_PATHS.keys()))
        ),
    )
    action: str = Field(
        description=(
            "One of: append (add to end of file), replace (whole-file "
            "replacement), append_to_section (add to a named `## Section`)."
        ),
    )
    content: str = Field(
        description=(
            "The new content to apply. For append/append_to_section: the "
            "text to add. For replace: the entire new file body. ≤8 KiB."
        ),
    )
    summary: str = Field(
        description=(
            "Operator-facing rationale (≤240 chars). What is this change "
            "and why? Operator reads this BEFORE expanding the diff."
        ),
    )
    section: str | None = Field(
        default=None,
        description=(
            "For append_to_section: the `## Section` heading to append "
            "under (e.g. 'Growth'). Ignored for append/replace."
        ),
    )


class ProposeChangeTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "asking-without-blocking"
    summary: ClassVar[str] = "Request a change to an operator-owned workspace file, gated on approval."
    use_when: ClassVar[str] = (
        "Use for any self-edit to a workspace document — how you sound, what "
        "you have learned about the operator, how you work. Nothing is applied "
        "until the operator approves the diff in the inbox."
    )
    not_when: ClassVar[str] = (
        "replying inside an existing comment thread, use `workspace_reply` or "
        "`agenda_comment`."
    )

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    @property
    def name(self) -> str:
        return "propose_change"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ProposeChangeInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, ProposeChangeInput)
            else ProposeChangeInput(**tool_input.model_dump())
        )

        content = inp.content or ""
        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            return ToolResult(
                output=(
                    f"content too large ({len(content.encode('utf-8'))} bytes "
                    f"> {_MAX_CONTENT_BYTES}). Split into smaller proposals."
                ),
                is_error=True,
            )
        summary = (inp.summary or "").strip()
        if not summary:
            return ToolResult(
                output="summary is required (operator-facing rationale)",
                is_error=True,
            )
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[:_MAX_SUMMARY_CHARS]

        try:
            full_path = validate_target(self._repo_root, inp.target_path)
            action = validate_action(inp.target_path, inp.action)
        except ProposeError as exc:
            return ToolResult(output=str(exc), is_error=True)

        try:
            before = full_path.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                output=f"failed to read {inp.target_path}: {exc}",
                is_error=True,
            )

        try:
            after = preview_change(
                current_text=before,
                action=action,
                content=content,
                section=inp.section,
            )
        except ProposeError as exc:
            return ToolResult(output=str(exc), is_error=True)

        spec = PROPOSABLE_PATHS[inp.target_path]
        label = str(spec["label"])
        diff = compute_diff(before, after, target_label=label)
        expected_hash_before = hash_text(before)

        event = WorkspaceEvent.new(
            kind="change_proposal",
            source="agent",
            title=f"Change request — {label}",
            summary=summary,
            payload={
                "target_path": inp.target_path,
                "label": label,
                "action": action,
                "content": content,
                "section": inp.section,
                "summary": summary,
                "expected_hash_before": expected_hash_before,
                "bytes_before": len(before.encode("utf-8")),
                "bytes_after": len(after.encode("utf-8")),
                "diff": diff,
            },
        )

        try:
            store = EventStore(workspace_events_dir())
            store.append_event(event)
        except OSError as exc:
            logger.exception("propose_change: workspace event append failed")
            return ToolResult(
                output=f"propose_change failed to write inbox event: {exc}",
                is_error=True,
            )

        return ToolResult(
            output=(
                f"Proposed change to {label} ({inp.target_path}). "
                f"Operator will approve or reject in the workspace inbox. "
                f"event_id={event.event_id}"
            ),
            metadata={
                "event_id": event.event_id,
                "target_path": inp.target_path,
                "action": action,
                "bytes_after": len(after.encode("utf-8")),
            },
        )
