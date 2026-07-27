"""skill_create tool — draft a new markdown skill under workspace/skills/.

Phase 4 (capability-growth), mirror of `agent_create` (Stage 10). TARS (or a
delegate) drafts a prose skill for a repeated chore. Attended sessions ASK
before the write; unattended, the executor's quarantine-write carve-out
(`headless_quarantine_write` ClassVar, honored by `permissions/decide.py` from
the CLASS only) lets the call proceed because the only write target is the
uninvokable quarantine below.

Quarantine: the skill is written to `workspace/skills/pending/<name>/SKILL.md`,
NOT directly to the active tree. `brain/skills.py::load_skills` skips
`pending/`, so a drafted skill never appears in the prompt manifest until the
operator promotes it (`skill_promote` or the Workspace `skill_approval` card).

Every successful draft files a `skill_approval` WorkspaceEvent — the operator's
proposal card in the Mirror Inbox. The pending file is canonical; the card is
best-effort.

Writes: workspace/skills/pending/<name>/SKILL.md + a skill_approval event.
Never edits or deletes existing skills.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal, Optional

import yaml
from pydantic import BaseModel, Field

from tesseract.brain.skills import (
    SKILL_FILENAME,
    SKILL_PENDING_DIRNAME,
    list_pending_skills,
    list_rejected_skills,
    list_skills_names,
    load_skill_folder,
    read_rejection_reason,
)
from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_skill_pending_cap,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.orchestrator.background_event_bus import get_background_bus
from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.broadcast import broadcast_workspace_event

logger = logging.getLogger(__name__)

# Agent Skills standard: name ≤ 64 chars, slug-style for the folder.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


class SkillCreateInput(BaseModel):
    name: str = Field(
        description="Slug-style name, lowercase, hyphen-separated. Unique. 2–64 chars."
    )
    description: str = Field(
        description=(
            "One-line description of what the skill does AND when to use it — "
            "this is what the manifest shows and how you decide to read it later."
        )
    )
    instructions: str = Field(
        description="The SKILL.md body — the markdown playbook TARS reads on demand."
    )
    rationale: str = Field(
        description="Why this skill is needed — shown to the operator at approval time."
    )
    proposer: Literal["entity", "claude", "codex", "user"] = Field(
        default="entity",
        description="Who is proposing this skill — recorded for review.",
    )
    version: str = Field(default="0.1")
    license: str | None = Field(default=None)
    allowed_tools: list[str] | None = Field(
        default=None,
        description="Optional Agent-Skills `allowed-tools` list — advisory only.",
    )


class SkillCreateTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "propose"
    # Phase 4 — decide.py quarantine-write carve-out: unattended calls may
    # proceed because every write lands in workspace/skills/pending/ (skipped
    # by the loader, uninvokable until promoted). Class-level on purpose:
    # decide.py reads `type(tool)`, so neither permissions.yaml nor an instance
    # attribute can flip it.
    headless_quarantine_write: ClassVar[bool] = True

    def __init__(
        self,
        skills_dir: Path,
        event_store: Optional[EventStore] = None,
        *,
        app_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        """``event_store`` receives the `skill_approval` proposal event;
        ``app_provider`` resolves the Mirror app at call time so the card
        broadcasts live to open Workspace tabs. Both optional — tests stay
        write-only."""
        self._skills_dir = skills_dir
        self._event_store = event_store
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "skill_create"

    @property
    def description(self) -> str:
        return (
            "Draft a new markdown skill under workspace/skills/. Attended: "
            "operator approves before the write. Unattended: the draft lands "
            "in the skills/pending/ quarantine and a proposal card is filed in "
            "the operator's Workspace Inbox — never surfaced in the prompt "
            "manifest until promoted. Never edits or deletes existing skills."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SkillCreateInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        # Always ASK — the executor handles the no-ask_fn case via the
        # quarantine-write carve-out. Returning PASSTHROUGH would let a
        # permissions.yaml override bypass the operator-approval invariant.
        return PermissionResult.ASK

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SkillCreateInput)
            else SkillCreateInput(**tool_input.model_dump())
        )

        # --- Validation (before any write) ---
        if not _NAME_RE.match(inp.name):
            return ToolResult(
                output=(
                    f"Invalid skill name {inp.name!r}. Must be lowercase, start "
                    "with a letter, hyphens allowed, 2–64 chars."
                ),
                is_error=True,
            )
        if not inp.instructions.strip():
            return ToolResult(output="instructions (the SKILL.md body) must not be empty.", is_error=True)

        if inp.name in list_skills_names(self._skills_dir):
            return ToolResult(
                output=f"Skill {inp.name!r} already exists in {self._skills_dir}.",
                is_error=True,
            )
        if inp.name in list_pending_skills(self._skills_dir):
            return ToolResult(
                output=(
                    f"Skill {inp.name!r} already pending promotion in "
                    f"{self._skills_dir}/{SKILL_PENDING_DIRNAME}/. Promote or remove it first."
                ),
                is_error=True,
            )
        if inp.name in list_rejected_skills(self._skills_dir):
            reason = read_rejection_reason(self._skills_dir, inp.name)
            return ToolResult(
                output=(
                    f"Skill {inp.name!r} was previously rejected by the operator"
                    + (f": {reason}" if reason else ".")
                    + " Address the rejection before re-proposing, or pick a different name."
                ),
                is_error=True,
            )

        # Headless flood guard — UNATTENDED drafts are capped by
        # runtime.yaml::skill_pending_cap. Attended drafts went through the
        # operator's ASK and stay uncapped.
        if context.ask_fn is None:
            cap = load_skill_pending_cap(default_runtime_config_path())
            pending_now = len(list_pending_skills(self._skills_dir))
            if pending_now >= cap:
                return ToolResult(
                    output=(
                        f"skills/pending/ already holds {pending_now} drafts "
                        f"(cap {cap}). Ask the operator to review the open "
                        "proposal cards before proposing more skills."
                    ),
                    is_error=True,
                )

        # --- Render + round-trip validation ---
        rendered = _render_skill_markdown(inp)
        roundtrip_error = _validate_roundtrip(rendered, inp.name)
        if roundtrip_error:
            return ToolResult(
                output=f"Rendered SKILL.md failed loader round-trip: {roundtrip_error}",
                is_error=True,
            )

        # --- Atomic write to quarantine ---
        pending_dir = self._skills_dir / SKILL_PENDING_DIRNAME / inp.name
        pending_dir.mkdir(parents=True, exist_ok=True)
        skill_path = pending_dir / SKILL_FILENAME
        try:
            _atomic_write(skill_path, rendered)
        except OSError as exc:
            return ToolResult(output=f"Failed to write skill file: {exc}", is_error=True)

        try:
            get_background_bus().publish(
                "SkillCreated", {"skill": inp.name, "proposer": inp.proposer},
            )
        except Exception:
            logger.warning("skill_create: bus publish failed for %s", inp.name, exc_info=True)

        # File the proposal card. The pending write above is canonical; the
        # card is best-effort (never let a notification failure lose the file).
        card_note = ""
        if self._event_store is not None:
            event = WorkspaceEvent.new(
                kind="skill_approval",
                source="tars",
                title=f"Skill proposal: {inp.name}",
                summary=inp.rationale,
                payload={
                    "name": inp.name,
                    "description": inp.description,
                    "rationale": inp.rationale,
                    "proposer": inp.proposer,
                    "rendered_markdown": rendered,
                    "session_id": context.session_id,
                },
            )
            try:
                self._event_store.append_event(event)
                card_note = f"\nProposal card filed in the Workspace Inbox ({event.event_id})."
            except Exception:
                logger.exception("skill_create: proposal event failed for %s", inp.name)
                card_note = (
                    "\nWARNING: the proposal card could not be filed in the "
                    "Workspace Inbox — post a workspace_post note so the "
                    "operator knows this skill is pending."
                )
            else:
                try:
                    if self._app_provider is not None:
                        app = self._app_provider()
                        if app is not None:
                            await broadcast_workspace_event(app, event)
                except Exception:
                    logger.warning(
                        "skill_create: card broadcast failed for %s",
                        inp.name, exc_info=True,
                    )

        logger.info("Skill created (pending): %s (%s)", inp.name, skill_path)
        return ToolResult(
            output=(
                f"Created skill (pending promotion): {inp.name}\n"
                f"File: {skill_path}\n\n"
                "The skill is quarantined — it does not appear in the prompt "
                "manifest until the operator promotes it (`skill_promote` or "
                "the Workspace proposal card)." + card_note
            )
        )


# ─── Helpers ─────────────────────────────────────────────


def _render_skill_markdown(inp: SkillCreateInput) -> str:
    """Render the full SKILL.md content. Frontmatter aligned to the Agent
    Skills standard (name/description required; version/license/allowed-tools
    optional). Pure function."""
    fm: dict[str, Any] = {"name": inp.name, "description": inp.description}
    if inp.version:
        fm["version"] = inp.version
    if inp.license:
        fm["license"] = inp.license
    if inp.allowed_tools:
        fm["allowed-tools"] = list(inp.allowed_tools)
    front = yaml.safe_dump(fm, sort_keys=False).rstrip()
    return f"---\n{front}\n---\n\n{inp.instructions.strip()}\n"


def _validate_roundtrip(rendered: str, name: str) -> str | None:
    """Write rendered SKILL.md to a temp folder, load via the skills loader,
    confirm it parses with the expected name. Returns an error message or None."""
    tmp_root = Path(tempfile.mkdtemp())
    folder = tmp_root / name
    folder.mkdir(parents=True, exist_ok=True)
    try:
        (folder / SKILL_FILENAME).write_text(rendered, encoding="utf-8")
        entry = load_skill_folder(folder)
        if entry is None:
            return "loader rejected the rendered SKILL.md (frontmatter/size)."
        if entry.name != name:
            return f"frontmatter name {entry.name!r} does not match folder {name!r}."
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


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via a .tmp intermediate."""
    tmp_path = path.with_suffix(".md.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def description_for_approval(inp: SkillCreateInput) -> str:
    """Return the input_summary for the permission engine approval prompt."""
    rendered = _render_skill_markdown(inp)
    return (
        f"Proposer: {inp.proposer}\n"
        f"Rationale: {inp.rationale}\n\n"
        f"--- Rendered SKILL.md ---\n\n{rendered}"
    )
