"""soul_growth_propose tool — propose a Growth bullet for SOUL.md.

Convenience wrapper around the generic `propose_change` mechanism. The
bullet is NOT written to SOUL.md by this tool — it is queued as a
`change_proposal` event in the workspace inbox. The operator approves
or rejects in the workspace; the REST handler performs the commit
through `apply_change()` (see `tesseract/kernel/workspace_changes.py`).

Posture: `auto`. The chat ASK gate was removed when the workspace
became the unified gate for any agent-initiated mutation of operator-
owned workspace files (soul, identity, foundation, …). Operator's
mental model: "the assistant is a colleague sending change requests; I review
in the workspace."

Bullet ergonomics preserved from the previous direct-write tool:
- ≤240 chars (Growth is a distillate, not a log).
- Appended to the `## Growth` section.
- Surfaces to operator via the workspace inbox; the post-approve
  commit broadcasts `soul_updated` so the Soul tab refreshes.
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

_SOUL_REL = "tesseract/workspace/SOUL.md"
_MAX_BULLET_CHARS = 240


class SoulGrowthProposeInput(BaseModel):
    bullet: str = Field(
        description=(
            "One distilled observation about you-with-this-operator "
            "(≤240 chars). Examples: 'Operator wants opinions stated, "
            "not menus offered. Give the answer in one sentence.' / "
            "'Dry humor lands well on tech topics; drop it in serious "
            "debugging.' Should be a STABLE pattern, not a one-off."
        ),
    )


class SoulGrowthProposeTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    """Queue a SOUL.md Growth bullet for operator approval (workspace inbox)."""

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    @property
    def name(self) -> str:
        return "soul_growth_propose"

    @property
    def description(self) -> str:
        return (
            "Queue a distilled observation for the SOUL.md Growth section. "
            "Use sparingly — Growth is a distillate (3-5 bullets total), "
            "not a log. Each bullet should be a stable pattern about "
            "you-with-this-operator. The bullet is NOT written immediately: "
            "it appears in the workspace inbox as a change_proposal row "
            "for the operator to Approve/Reject. On Approve, SOUL.md is "
            "updated and `soul_updated` fires."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SoulGrowthProposeInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SoulGrowthProposeInput)
            else SoulGrowthProposeInput(**tool_input.model_dump())
        )

        bullet = (inp.bullet or "").strip().lstrip("-•*").strip()
        if not bullet:
            return ToolResult(output="Empty bullet — nothing proposed.", is_error=True)
        if len(bullet) > _MAX_BULLET_CHARS:
            return ToolResult(
                output=(
                    f"Bullet too long ({len(bullet)} chars > {_MAX_BULLET_CHARS}). "
                    "Growth is a distillate. Trim to one observation."
                ),
                is_error=True,
            )

        try:
            full_path = validate_target(self._repo_root, _SOUL_REL)
            action = validate_action(_SOUL_REL, "append_to_section")
        except ProposeError as exc:
            return ToolResult(output=str(exc), is_error=True)

        try:
            before = full_path.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(output=f"SOUL.md read failed: {exc}", is_error=True)

        bullet_line = f"- {bullet}\n"
        try:
            after = preview_change(
                current_text=before,
                action=action,
                content=bullet_line,
                section="Growth",
            )
        except ProposeError as exc:
            return ToolResult(output=str(exc), is_error=True)

        label = str(PROPOSABLE_PATHS[_SOUL_REL]["label"])
        diff = compute_diff(before, after, target_label=label)
        expected_hash_before = hash_text(before)

        event = WorkspaceEvent.new(
            kind="change_proposal",
            source="agent",
            title=f"Soul growth bullet — {bullet[:80]}",
            summary=bullet,
            payload={
                "target_path": _SOUL_REL,
                "label": label,
                "action": action,
                "content": bullet_line,
                "section": "Growth",
                "summary": bullet,
                "expected_hash_before": expected_hash_before,
                "bytes_before": len(before.encode("utf-8")),
                "bytes_after": len(after.encode("utf-8")),
                "diff": diff,
                "kind_origin": "soul_growth",
            },
        )

        try:
            store = EventStore(workspace_events_dir())
            store.append_event(event)
        except OSError as exc:
            logger.exception("soul_growth_propose: workspace event append failed")
            return ToolResult(
                output=f"failed to write inbox event: {exc}",
                is_error=True,
            )

        return ToolResult(
            output=(
                f"Soul growth bullet queued for approval ({len(bullet)} chars). "
                f"Operator approves in workspace; SOUL.md updates on commit. "
                f"event_id={event.event_id}"
            ),
            metadata={
                "event_id": event.event_id,
                "target_path": _SOUL_REL,
                "bullet": bullet,
            },
        )
