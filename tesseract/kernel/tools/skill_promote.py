"""skill_promote tool — move a quarantined skill into the active set.

Phase 4 (capability-growth), mirror of `agent_promote` / W7-A. `skill_create`
writes new skills under `workspace/skills/pending/<name>/` so they cannot be
surfaced live even if the ASK gate is bypassed. `skill_promote` is the explicit
operator action that moves a pending skill into the active `workspace/skills/`
tree. Returns ASK so the executor consults the operator even if a posture
override would otherwise auto-allow.

Unlike agents there is no INDEX.md to append — the skills manifest is derived
from the loader at prompt-build time, so promotion is a single atomic
directory move.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.brain.skills import (
    SkillEntry,
    SKILL_PENDING_DIRNAME,
    SKILL_REJECTED_DIRNAME,
    list_pending_skills,
    list_rejected_skills,
    list_skills_names,
    load_skill_folder,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.workspace_events import EventStore

logger = logging.getLogger(__name__)


def promote_pending_skill(
    skills_dir: Path, name: str,
) -> tuple[SkillEntry | None, str | None]:
    """Move `pending/<name>/` into the active skills tree.

    The single promotion implementation shared by the `skill_promote` chat
    tool and the Workspace `skill_approval` card's approve route. Returns
    ``(entry, None)`` on success, ``(None, error)`` on failure.
    """
    if name not in list_pending_skills(skills_dir):
        pending = list_pending_skills(skills_dir)
        return None, (
            f"No pending skill {name!r} in {skills_dir / SKILL_PENDING_DIRNAME}. "
            f"Pending: {pending or '(none)'}"
        )

    if name in list_skills_names(skills_dir):
        return None, (
            f"Skill {name!r} already active. Remove it first if you want to "
            "replace it with the pending version."
        )

    src = skills_dir / SKILL_PENDING_DIRNAME / name
    # Validate the draft parses cleanly before moving anything — a malformed
    # pending skill should fail loudly here, not become an inert active folder.
    entry = load_skill_folder(src)
    if entry is None:
        return None, (
            f"Pending skill {name!r} failed validation (missing/oversize/"
            "invalid SKILL.md frontmatter). Fix the draft before promoting."
        )
    # Defense against a hand-placed pending folder whose frontmatter `name`
    # diverges from its dirname: the active manifest is keyed by frontmatter
    # name, so a mismatch would let a draft masquerade under the wrong slug.
    # skill_create enforces equality at draft time; enforce it again here.
    if entry.name != name:
        return None, (
            f"Pending skill folder {name!r} declares frontmatter name "
            f"{entry.name!r} — they must match. Fix the draft before promoting."
        )

    dst = skills_dir / name
    try:
        os.replace(str(src), str(dst))
    except OSError as exc:
        return None, f"Failed to promote {name!r}: {exc}"

    logger.info("Skill promoted: %s (%s)", name, dst)
    return entry, None


def archive_rejected_skill(
    skills_dir: Path, name: str, reason: str | None,
) -> str | None:
    """Move `pending/<name>/` into `rejected/` and write a reason sidecar.

    Returns an error string on failure, None on success. Mirrors the agent
    reject path (`routes/workspace.py::_archive_rejected_agent`).
    """
    src = skills_dir / SKILL_PENDING_DIRNAME / name
    if not (src / "SKILL.md").exists():
        return f"No pending skill {name!r} to reject in {src}."

    rejected_dir = skills_dir / SKILL_REJECTED_DIRNAME
    rejected_dir.mkdir(parents=True, exist_ok=True)
    dst = rejected_dir / name
    # A prior rejection of the same name would block the move — clear it so
    # re-proposal + re-rejection stays idempotent (the reason sidecar below
    # records the latest decision).
    if dst.exists():
        import shutil

        shutil.rmtree(dst, ignore_errors=True)
    try:
        os.replace(str(src), str(dst))
    except OSError as exc:
        return f"Failed to archive rejected skill {name!r}: {exc}"

    if reason:
        try:
            (rejected_dir / f"{name}.reason.txt").write_text(
                reason.strip() + "\n", encoding="utf-8",
            )
        except OSError:
            logger.warning("skill reject: reason sidecar write failed for %s", name)
    return None


class SkillPromoteInput(BaseModel):
    name: str = Field(description="Slug of the pending skill to promote.")


class SkillPromoteTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    def __init__(self, skills_dir: Path, event_store: Optional[EventStore] = None) -> None:
        """``event_store`` lets a chat-side promotion settle any open
        `skill_approval` card for the same skill, so the two approval surfaces
        (tool + Workspace card) converge. Optional — tests without a store
        skip the settle."""
        self._skills_dir = skills_dir
        self._event_store = event_store

    @property
    def name(self) -> str:
        return "skill_promote"

    @property
    def description(self) -> str:
        return (
            "Promote a quarantined skill from workspace/skills/pending/ to the "
            "active set. Required before a newly drafted skill is surfaced in "
            "the prompt manifest. Operator-approval-gated."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SkillPromoteInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.ASK

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SkillPromoteInput)
            else SkillPromoteInput(**tool_input.model_dump())
        )

        entry, err = promote_pending_skill(self._skills_dir, inp.name)
        if err is not None:
            return ToolResult(output=err, is_error=True)

        # Settle any open proposal card for this skill so the Workspace Inbox
        # doesn't keep offering a promotion that already happened chat-side.
        # Best-effort: a store failure must not fail a promotion already done
        # on disk.
        if self._event_store is not None:
            try:
                open_cards = [
                    ev for ev in self._event_store.list_events(
                        kinds=("skill_approval",), status="pending",
                    )
                    if (ev.payload or {}).get("name") == inp.name
                ]
                for ev in open_cards:
                    self._event_store.update_event_status(
                        ev.event_id, "applied",
                        reason="promoted via skill_promote",
                    )
            except Exception:
                logger.warning(
                    "skill_promote: card settle failed for %s",
                    inp.name, exc_info=True,
                )

        dst = self._skills_dir / inp.name
        logger.info("Skill promoted: %s (%s)", inp.name, dst)
        return ToolResult(
            output=(
                f"Promoted skill: {inp.name}\n"
                f"Active path: {dst / 'SKILL.md'}\n"
                "It now appears in the prompt manifest and is readable with "
                "`file_read`."
            )
        )
