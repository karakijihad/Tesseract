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
- ≤240 chars (each section is a distillate, not a log).
- Appended to the named growth section (`SOUL_GROWTH_SECTIONS`).
- Surfaces to operator via the workspace inbox; the post-approve
  commit broadcasts `soul_updated` so the Soul tab refreshes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.kernel.workspace_changes import (
    PROPOSABLE_PATHS,
    SOUL_GROWTH_SECTIONS,
    ProposeError,
    compute_diff,
    document_posture,
    hash_text,
    preview_change,
    settle_proposal,
    validate_action,
    validate_growth_section,
    validate_target,
    workspace_events_dir,
)
from tesseract.workspace_events.events import EventStore, WorkspaceEvent

logger = logging.getLogger(__name__)

_SOUL_REL = "tesseract/workspace/SOUL.md"
_MAX_BULLET_CHARS = 240


_SECTION_GUIDE = "; ".join(
    f"{name} = {purpose}" for name, purpose in SOUL_GROWTH_SECTIONS.items()
)


class SoulGrowthProposeInput(BaseModel):
    section: str = Field(
        description=(
            "Which part of yourself this belongs to. " + _SECTION_GUIDE + ". "
            "Pick the one it actually is: a lesson about how you work is Craft "
            "even when it arrived as a correction about tone."
        ),
    )
    bullet: str = Field(
        description=(
            "One distilled observation, written in the first person, ≤240 "
            "chars. Examples: 'They want opinions stated, not menus offered. "
            "Give the answer in one sentence.' / 'I check the log before I "
            "defend an assumption.' A STABLE pattern, not a one-off."
        ),
    )


class SoulGrowthProposeTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "remembering"
    summary: ClassVar[str] = "Queue a distilled SOUL.md Growth bullet for operator approval."
    use_when: ClassVar[str] = (
        "Use sparingly for a stable, distilled pattern about you-with-this-"
        "operator. Queues a workspace-inbox proposal; SOUL.md updates only on "
        "operator approve."
    )
    not_when: ClassVar[str] = (
        "use `diary_append` for a single session's observation; use "
        "`memory_save` for facts about the operator or project."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "queryable"

    """Queue a SOUL.md Growth bullet for operator approval (workspace inbox)."""

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    @property
    def name(self) -> str:
        return "soul_growth_propose"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SoulGrowthProposeInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SoulGrowthProposeInput)
            else SoulGrowthProposeInput(**tool_input.model_dump())
        )

        # SOUL.md is who the assistant is, and under `free` a proposal against
        # it applies without an operator seeing it (`permissions.yaml`'s
        # `workspace_documents` holds OPERATING, WORKSHOP and CHANNEL at ask
        # and not this one). A turn nobody asked for, whose prompt is built
        # from files an outside tool may have written, is not a turn that gets
        # to say what the assistant is like.
        if context.session_kind == "autonomy":
            return ToolResult(
                output=(
                    "Nobody is watching this conversation, so it cannot propose "
                    "a change to who you are. Write the observation in the "
                    "diary and let a reflection the operator is part of decide "
                    "whether it is a pattern."
                ),
                is_error=True,
                caller_error=True,
                metadata={"refused_unattended": True},
            )

        try:
            section = validate_growth_section((inp.section or "").strip())
        except ProposeError as exc:
            return ToolResult(output=str(exc), is_error=True, caller_error=True)

        bullet = (inp.bullet or "").strip().lstrip("-•*").strip()
        if not bullet:
            return ToolResult(
                output="Empty bullet — nothing proposed.",
                is_error=True,
                caller_error=True,
            )
        if len(bullet) > _MAX_BULLET_CHARS:
            return ToolResult(
                output=(
                    f"Bullet too long ({len(bullet)} chars > {_MAX_BULLET_CHARS}). "
                    "Growth is a distillate. Trim to one observation."
                ),
                is_error=True,
                caller_error=True,
            )

        try:
            full_path = validate_target(self._repo_root, _SOUL_REL)
            action = validate_action(_SOUL_REL, "append_to_section")
        except ProposeError as exc:
            return ToolResult(output=str(exc), is_error=True, caller_error=True)

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
                section=section,
            )
        except ProposeError as exc:
            return ToolResult(output=str(exc), is_error=True, caller_error=True)

        label = str(PROPOSABLE_PATHS[_SOUL_REL]["label"])
        diff = compute_diff(before, after, target_label=label)
        expected_hash_before = hash_text(before)

        event = WorkspaceEvent.new(
            kind="change_proposal",
            source="agent",
            title=f"Soul · {section} — {bullet[:70]}",
            summary=bullet,
            payload={
                "target_path": _SOUL_REL,
                "label": label,
                "action": action,
                "content": bullet_line,
                "section": section,
                "summary": bullet,
                "expected_hash_before": expected_hash_before,
                "bytes_before": len(before.encode("utf-8")),
                "bytes_after": len(after.encode("utf-8")),
                "diff": diff,
                "kind_origin": "soul_growth",
            },
        )

        # The same door `propose_change` goes through, and the same reader for
        # the posture. Two callers deriving the same answer separately is how
        # one file ends up auto for one tool and gated for the other.
        posture = document_posture(context, _SOUL_REL)
        event, applied, error = settle_proposal(
            event=event,
            target_path=_SOUL_REL,
            action=action,
            content=bullet_line,
            section=section,
            expected_hash_before=expected_hash_before,
            posture=posture,
        )
        if error is not None:
            return ToolResult(output=f"soul_growth_propose: {error}", is_error=True)

        try:
            store = EventStore(workspace_events_dir())
            store.append_event(event)
        except OSError as exc:
            logger.exception("soul_growth_propose: workspace event append failed")
            return ToolResult(
                output=f"failed to write inbox event: {exc}",
                is_error=True,
            )

        if applied is not None:
            settled = (
                f"No change: {applied.no_op_reason}."
                if applied.no_op_reason
                else f"Written into SOUL.md under {section} ({len(bullet)} chars)."
            )
            note = f"{settled} Filed in the workspace inbox as history."
        else:
            note = (
                f"Soul growth bullet queued for approval ({len(bullet)} chars). "
                f"Operator approves in workspace; SOUL.md updates on commit."
            )

        return ToolResult(
            output=f"{note} event_id={event.event_id}",
            receipt=Receipt(
                kind="record",
                id=event.event_id,
                locator=str(store.events_path),
            ),
            metadata={
                "event_id": event.event_id,
                "target_path": _SOUL_REL,
                "bullet": bullet,
                "posture": posture,
                "status": event.status,
            },
        )
