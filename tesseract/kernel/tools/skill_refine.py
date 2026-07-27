"""skill_refine tool — propose a revised body for an existing active skill.

Phase 4 (capability-growth) follow-up. The chat-side companion to the
`skill_refinement` scheduler job: when TARS notices a live skill's instructions
are stale or wrong, it drafts an improved SKILL.md and files a `skill_refinement`
card. It does NOT apply the change — activation stays with the operator (the
card's approve route overwrites the live SKILL.md). Same invariant as every
Phase-4 surface: no skill mutates live without operator approval.

Files a `skill_refinement` WorkspaceEvent with `{name, current_markdown,
proposed_markdown}`; the pending file is the LIVE skill (unchanged until
approve). Dedups against an already-open card for the same skill.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Callable, ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.brain.skills import (
    SKILL_FILENAME,
    list_skills_names,
    load_skill_folder,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.broadcast import broadcast_workspace_event

logger = logging.getLogger(__name__)


class SkillRefineInput(BaseModel):
    name: str = Field(description="Slug of the ACTIVE skill to refine.")
    proposed_markdown: str = Field(
        description=(
            "The full revised SKILL.md (frontmatter + body). Its frontmatter "
            "`name` must equal `name`. Applied to the live skill only on "
            "operator approval."
        )
    )
    rationale: str = Field(
        description="Why the skill needs revising — shown to the operator."
    )


class SkillRefineTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "propose"

    def __init__(
        self,
        skills_dir: Path,
        event_store: Optional[EventStore] = None,
        *,
        app_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._skills_dir = skills_dir
        self._event_store = event_store
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "skill_refine"

    @property
    def description(self) -> str:
        return (
            "Propose a revised SKILL.md for an existing active skill. Files a "
            "skill_refinement card in the operator's Workspace Inbox — the live "
            "skill is NOT changed until the operator approves. Use when a "
            "skill's instructions are stale, wrong, or caused repeated failures."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SkillRefineInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.ASK

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SkillRefineInput)
            else SkillRefineInput(**tool_input.model_dump())
        )

        if inp.name not in list_skills_names(self._skills_dir):
            return ToolResult(
                output=(
                    f"No active skill {inp.name!r} to refine. Only existing "
                    "skills can be refined; use skill_create for a new one."
                ),
                is_error=True,
            )

        validation_error = _validate_proposal(inp.name, inp.proposed_markdown)
        if validation_error:
            return ToolResult(output=validation_error, is_error=True)

        if self._event_store is None:
            return ToolResult(
                output="No workspace event store wired — cannot file a refinement card.",
                is_error=True,
            )

        # Dedup: don't stack a second open card for the same skill.
        try:
            open_cards = [
                ev for ev in self._event_store.list_events(
                    kinds=("skill_refinement",), status="pending",
                )
                if (ev.payload or {}).get("name") == inp.name
            ]
        except Exception:
            open_cards = []
        if open_cards:
            return ToolResult(
                output=(
                    f"A refinement card for {inp.name!r} is already open "
                    f"({open_cards[0].event_id}). Resolve it before proposing another."
                ),
                is_error=True,
            )

        current = _read_current(self._skills_dir, inp.name)
        event = WorkspaceEvent.new(
            kind="skill_refinement",
            source="tars",
            title=f"Skill refinement: {inp.name}",
            summary=inp.rationale,
            payload={
                "name": inp.name,
                "current_markdown": current,
                "proposed_markdown": inp.proposed_markdown,
                "origin": "skill_refine",
            },
        )
        try:
            self._event_store.append_event(event)
        except Exception:
            logger.exception("skill_refine: append card failed for %s", inp.name)
            return ToolResult(output="Failed to file the refinement card.", is_error=True)

        try:
            if self._app_provider is not None:
                app = self._app_provider()
                if app is not None:
                    await broadcast_workspace_event(app, event)
        except Exception:
            logger.warning("skill_refine: broadcast failed for %s", inp.name, exc_info=True)

        return ToolResult(
            output=(
                f"Filed a refinement proposal for {inp.name} ({event.event_id}). "
                "The live skill is unchanged until the operator approves the card."
            )
        )


def _validate_proposal(name: str, proposed: str) -> str | None:
    """Round-trip the proposed SKILL.md; return an error message or None."""
    if not proposed.strip():
        return "proposed_markdown must not be empty."
    tmp_root = Path(tempfile.mkdtemp())
    folder = tmp_root / name
    folder.mkdir(parents=True, exist_ok=True)
    try:
        (folder / SKILL_FILENAME).write_text(proposed, encoding="utf-8")
        entry = load_skill_folder(folder)
        if entry is None:
            return "proposed_markdown failed loader validation (frontmatter/size)."
        if entry.name != name:
            return f"proposed frontmatter name {entry.name!r} must match {name!r}."
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    finally:
        try:
            (folder / SKILL_FILENAME).unlink(missing_ok=True)
            folder.rmdir()
            tmp_root.rmdir()
        except OSError:
            pass


def _read_current(skills_dir: Path, name: str) -> str:
    try:
        return (skills_dir / name / SKILL_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return ""
