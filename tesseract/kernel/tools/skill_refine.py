"""skill_refine tool — revise an existing active skill.

The chat-side companion to the `skill_refinement` scheduler job: when the
assistant notices a live skill's instructions are stale or wrong, it drafts an
improved SKILL.md and this tool replaces the live one.

**The gate is the approval.** The tool's posture is whatever `permissions.yaml`
says for the mode the install runs in: `ask` where the operator keeps the
decision, so the prompt they answer IS the approval, and `auto` where they have
given the assistant that decision. It used to file a card the operator had to
click regardless of mode, which meant an install set to run on its own still
stopped here, and a hardcoded ASK bypassed the file that is the authority on
what asks.

The revision goes through `brain/skills.py::replace_skill_body`, the one path
that changes a live skill, so a playbook's previous revision is kept under
`history/<version>/` and a proposal that is not a later revision is refused
before anything is touched. A `skill_refinement` card is still filed, as the
record of what changed and why, and it is settled as applied.

Unattended (no operator on any surface) this is refused like any other write.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.brain.skills import (
    SKILL_FILENAME,
    list_skills_names,
    load_skill_folder,
    replace_skill_body,
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
            "`name` must equal `name`. For a playbook, `version` must be a "
            "whole number greater than the live one; the live revision is kept."
        )
    )
    rationale: str = Field(
        description="Why the skill needs revising. Recorded on the card."
    )


class SkillRefineTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "extending-yourself"
    summary: ClassVar[str] = "Revise an existing active skill or playbook."
    use_when: ClassVar[str] = (
        "Use when a live skill's instructions are stale, wrong, or caused "
        "repeated failures, or when a playbook needs a step changed. Replaces "
        "the live file; a playbook's previous revision is kept."
    )
    not_when: ClassVar[str] = (
        "for a skill that does not exist yet, use `skill_create` instead. "
        "this tool only refines an already-active one."
    )
    depends_on: ClassVar[str] = ""

    def __init__(
        self,
        skills_dir: Path,
        event_store: Optional[EventStore] = None,
        *,
        app_provider: Optional[Callable[[], Any]] = None,
        tool_names: Optional[Callable[[], Optional[frozenset[str]]]] = None,
    ) -> None:
        self._skills_dir = skills_dir
        self._event_store = event_store
        self._app_provider = app_provider
        # The live registry's names, for the door a revised playbook goes
        # through: a step naming a tool the runtime does not have is refused
        # here as it is in `skill_create`.
        self._tool_names = tool_names or (lambda: None)

    @property
    def name(self) -> str:
        return "skill_refine"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SkillRefineInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        # `permissions.yaml` decides, per mode. See the module docstring.
        return PermissionResult.PASSTHROUGH

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
        if not inp.proposed_markdown.strip():
            return ToolResult(output="proposed_markdown must not be empty.", is_error=True)

        current = _read_current(self._skills_dir, inp.name)
        err = replace_skill_body(
            self._skills_dir, inp.name, inp.proposed_markdown, tool_names=self._tool_names()
        )
        if err is not None:
            return ToolResult(output=f"Not applied: {err}", is_error=True)

        revised = load_skill_folder(self._skills_dir / inp.name)
        version = f" v{revised.version}" if revised is not None and revised.version else ""
        note = ""
        if self._event_store is not None:
            event = WorkspaceEvent.new(
                kind="skill_refinement",
                source="agent",
                title=f"Skill revised: {inp.name}{version}",
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
                self._event_store.update_event_status(
                    event.event_id, "applied", reason="applied by skill_refine"
                )
                note = f" Recorded as {event.event_id}."
            except Exception:
                logger.exception("skill_refine: recording the revision failed for %s", inp.name)
                note = " The revision is live; recording it in the Inbox failed."
            else:
                try:
                    if self._app_provider is not None:
                        app = self._app_provider()
                        if app is not None:
                            await broadcast_workspace_event(app, event)
                except Exception:
                    logger.warning("skill_refine: broadcast failed for %s", inp.name, exc_info=True)

        return ToolResult(output=f"Revised {inp.name}{version}; the live skill is updated.{note}")


def _read_current(skills_dir: Path, name: str) -> str:
    try:
        return (skills_dir / name / SKILL_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return ""
