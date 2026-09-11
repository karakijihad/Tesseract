"""skill_create tool — draft a new markdown skill under workspace/skills/.

Mirror of `agent_create`. The assistant (or a delegate) drafts a prose
skill for a repeated chore, or a PLAYBOOK: a skill that also declares the
procedure contract (`brain/skills.py::PLAYBOOK_KEYS`), which is how a way of
doing something that worked is written down so the next time it is asked for
the steps are already known. Attended sessions get the posture
`permissions.yaml` sets for the mode (`ask` where the operator keeps the
decision, `auto` where they gave it away); unattended, the executor's
quarantine-write carve-out (`headless_quarantine_write` ClassVar, honored by
`permissions/decide.py` from the CLASS only) lets the call proceed because the
only write target is the uninvokable quarantine below.

**A playbook is refused at the door, not after the operator has read it.**
The contract is checked before the write (`playbook_contract.gaps_for_skill`
with the live registry): a step naming a tool the runtime does not have or
one the playbook itself forbids, a status outside the vocabulary. The version
gap cannot fire here any more, because a created playbook is stamped `1` by
`render_skill_markdown` rather than taking one from the caller. And any field
naming a credential-bearing path or a
path outside the home tree is refused outright, because a generated
procedure is untrusted until checked and the operator should never be handed
one that can only do harm.

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
    _FRONTMATTER_RE,
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
from tesseract.kernel.tools.receipt import Receipt
from tesseract.kernel.tools.skill_promote import promote_pending_skill, promotion_is_auto
from tesseract.orchestrator.background_event_bus import get_background_bus
from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.broadcast import broadcast_workspace_event

logger = logging.getLogger(__name__)

# Agent Skills standard: name ≤ 64 chars, slug-style for the folder.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


class PlaybookStep(BaseModel):
    do: str = Field(description="What this step does.")
    tool: str = Field(default="", description="The tool it uses, or empty for a reasoning step.")


class SkillCreateInput(BaseModel):
    name: str = Field(
        description="Slug-style name, lowercase, hyphen-separated. Unique. 2 to 64 characters."
    )
    description: str = Field(
        description=(
            "One line saying what the skill does AND when to use it. "
            "this is what the manifest shows and how you decide to read it later."
        )
    )
    instructions: str = Field(
        description="The SKILL.md body: the markdown playbook the assistant reads on demand."
    )
    rationale: str = Field(
        description="Why this skill is needed. The operator reads this when approving it."
    )
    proposer: Literal["entity", "claude", "codex", "user"] = Field(
        default="entity",
        description="Who is proposing this skill. Recorded for review.",
    )
    version: str = Field(
        default="0.1",
        description=(
            "Interop field for a plain skill, free form. A playbook ignores "
            "it: a new playbook is version 1 and the runtime writes it."
        ),
    )
    license: str | None = Field(default=None)
    allowed_tools: list[str] | None = Field(
        default=None,
        description=(
            "Agent-Skills `allowed-tools`. For a playbook, every tool a step "
            "uses must be listed here."
        ),
    )
    # The playbook half. Giving `trigger` (or any of these) makes the skill a
    # playbook, and then the whole contract is owed and checked before the
    # write. A plain skill leaves them all unset.
    trigger: str | None = Field(
        default=None,
        description=(
            "The shape of problem this answers, in the operator's words, e.g. "
            "'a brief on a topic, sent to the phone'. Setting it makes this a "
            "playbook and every field below is then required."
        ),
    )
    use_when: str | None = Field(default=None, description="When to reach for it.")
    not_when: str | None = Field(default=None, description="When not to, even if it looks close.")
    preconditions: list[str] | None = Field(
        default=None, description="What must be true before step one. May be empty.",
    )
    steps: list[PlaybookStep] | None = Field(
        default=None, description="Ordered steps, each naming the tool it uses, or none.",
    )
    forbidden_tools: list[str] | None = Field(
        default=None, description="Tools this playbook must never use. May be empty.",
    )
    expected_result: str | None = Field(
        default=None, description="What done looks like, so a run can be graded.",
    )
    failure_modes: list[str] | None = Field(
        default=None, description="What goes wrong and what to do instead. May be empty.",
    )
    evidence: list[str] | None = Field(
        default=None,
        description="Turn ids this was learned from. Empty for one written by hand.",
    )
    confidence: float | None = Field(
        default=None, description="Only where something measured it. Leave unset otherwise.",
    )


class SkillCreateTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "propose"
    # decide.py quarantine-write carve-out: unattended calls may
    # proceed because every write lands in workspace/skills/pending/ (skipped
    # by the loader, uninvokable until promoted). Class-level on purpose:
    # decide.py reads `type(tool)`, so neither permissions.yaml nor an instance
    # attribute can flip it.
    headless_quarantine_write: ClassVar[bool] = True

    group: ClassVar[str] = "extending-yourself"
    summary: ClassVar[str] = "Draft a new skill or playbook into the pending quarantine."
    use_when: ClassVar[str] = (
        "Use to write down a brand-new skill for a repeated chore, or a "
        "playbook once a way of doing something has worked: give `trigger`, "
        "`use_when`, the `steps` with their tools and `expected_result`. "
        "Writes to skills/pending/ and files a card; live once promoted."
    )
    not_when: ClassVar[str] = (
        "to activate a drafted skill, use `skill_promote`; to improve an "
        "existing active skill, use `skill_refine`."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "idempotent"

    def __init__(
        self,
        skills_dir: Path,
        event_store: Optional[EventStore] = None,
        *,
        app_provider: Optional[Callable[[], Any]] = None,
        tool_names: Optional[Callable[[], frozenset[str]]] = None,
        registry_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        """``event_store`` receives the `skill_approval` proposal event;
        ``app_provider`` resolves the Mirror app at call time so the card
        broadcasts live to open Workspace tabs; ``tool_names`` is the live
        registry's names, asked at call time, so a playbook step naming a tool
        that is not there is refused. All optional — tests stay write-only,
        and with no registry a step tool is not checked."""
        self._skills_dir = skills_dir
        self._event_store = event_store
        self._app_provider = app_provider
        self._tool_names = tool_names or (lambda: None)
        # For the one question after the write: does the file let a draft be
        # promoted without a hand? Asked of the live registry's policy.
        self._registry_provider = registry_provider or (lambda: None)

    @property
    def name(self) -> str:
        return "skill_create"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SkillCreateInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        # `permissions.yaml` decides, per mode. The unattended case is still
        # the executor's quarantine-write carve-out: nothing here reaches the
        # active tree.
        return PermissionResult.PASSTHROUGH

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
                caller_error=True,
            )
        if not inp.instructions.strip():
            return ToolResult(
                output="instructions (the SKILL.md body) must not be empty.",
                is_error=True,
                caller_error=True,
            )

        if inp.name in list_skills_names(self._skills_dir):
            return ToolResult(
                output=f"Skill {inp.name!r} already exists in {self._skills_dir}.",
                is_error=True,
                caller_error=True,
            )
        if inp.name in list_pending_skills(self._skills_dir):
            return ToolResult(
                output=(
                    f"Skill {inp.name!r} already pending promotion in "
                    f"{self._skills_dir}/{SKILL_PENDING_DIRNAME}/. Promote or remove it first."
                ),
                is_error=True,
                caller_error=True,
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
                caller_error=True,
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
                    caller_error=True,
                )

        # --- Render + round-trip validation ---
        rendered = render_skill_markdown(inp)
        roundtrip_error = _validate_roundtrip(rendered, inp.name)
        if roundtrip_error:
            return ToolResult(
                output=f"Rendered SKILL.md failed loader round-trip: {roundtrip_error}",
                is_error=True,
                caller_error=True,
            )
        refused = refuse_playbook(rendered, inp.name, self._tool_names())
        if refused:
            return ToolResult(output=refused, is_error=True, caller_error=True)

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
                source="agent",
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
                    "Workspace Inbox. Post a workspace_post note so the "
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

        # Attended, and the file says a promotion needs no hand: promote now,
        # so a draft does not sit in pending waiting for a card nobody is
        # asked to read. Unattended stays in quarantine whatever the file
        # says, which is the carve-out that let the write happen at all.
        if context.ask_fn is not None and promotion_is_auto(self._registry_provider()):
            entry, err = promote_pending_skill(self._skills_dir, inp.name)
            if err is None and entry is not None:
                self._settle_card(inp.name)
                return ToolResult(
                    output=(
                        f"Created and activated skill: {inp.name}\n"
                        f"File: {self._skills_dir / inp.name / SKILL_FILENAME}\n\n"
                        "Promotion needs no approval in this mode, so it is live "
                        "now and listed in your prompt from the next turn." + card_note
                    ),
                    receipt=Receipt(
                        kind="record",
                        id=inp.name,
                        locator=str(
                            self._skills_dir / inp.name / SKILL_FILENAME
                        ),
                    ),
                )
            logger.warning("skill_create: auto promotion of %s failed: %s", inp.name, err)

        return ToolResult(
            output=(
                f"Created skill (pending promotion): {inp.name}\n"
                f"File: {skill_path}\n\n"
                "The skill is quarantined: it does not appear in the prompt "
                "manifest until the operator promotes it (`skill_promote` or "
                "the Workspace proposal card)." + card_note
            ),
            receipt=Receipt(kind="record", id=inp.name, locator=str(skill_path)),
        )

    def _settle_card(self, name: str) -> None:
        """Mark the just-filed proposal card approved, so the inbox does not
        offer a decision that has already been taken."""
        if self._event_store is None:
            return
        try:
            for ev in self._event_store.list_events(kinds=("skill_approval",), status="pending"):
                if (ev.payload or {}).get("name") == name:
                    self._event_store.update_event_status(
                        ev.event_id, "approved", reason="promoted on its own: skill_promote is auto in this mode"
                    )
        except Exception:
            logger.warning("skill_create: could not settle the card for %s", name, exc_info=True)


# ─── Helpers ─────────────────────────────────────────────


def render_skill_markdown(inp: SkillCreateInput) -> str:
    """Render the full SKILL.md content. Frontmatter aligned to the Agent
    Skills standard (name/description required; version/license/allowed-tools
    optional). Pure function."""
    fm: dict[str, Any] = {"name": inp.name, "description": inp.description}
    playbook = _is_playbook(inp)
    if playbook:
        # A playbook's version is an ordering key, so it is the runtime's and
        # not the author's: `keep_predecessor` archives under it and refuses
        # anything that does not sort above the live one. This tool CREATES,
        # and a created playbook is the first revision. Reading `inp.version`
        # here coerced a semantic one to "1" without saying so, which put a
        # revision of a v3 playbook below its own predecessor.
        fm["version"] = "1"
        fm["status"] = "draft"
    elif inp.version:
        fm["version"] = inp.version
    if inp.license:
        fm["license"] = inp.license
    if inp.allowed_tools or playbook:
        fm["allowed-tools"] = list(inp.allowed_tools or [])
    if playbook:
        # Every contract key is written, empty where the author gave nothing,
        # so the contract reports "declared and empty" rather than "missing"
        # and the file reads as a whole declaration.
        fm["trigger"] = inp.trigger or ""
        fm["use_when"] = inp.use_when or ""
        fm["not_when"] = inp.not_when or ""
        fm["preconditions"] = list(inp.preconditions or [])
        fm["steps"] = [
            {"do": step.do, **({"tool": step.tool} if step.tool else {})}
            for step in (inp.steps or [])
        ]
        fm["forbidden-tools"] = list(inp.forbidden_tools or [])
        fm["expected_result"] = inp.expected_result or ""
        fm["failure_modes"] = list(inp.failure_modes or [])
        fm["evidence"] = list(inp.evidence or [])
        fm["confidence"] = inp.confidence
    front = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{front}\n---\n\n{inp.instructions.strip()}\n"


def _is_playbook(inp: SkillCreateInput) -> bool:
    return any(
        getattr(inp, key) is not None
        for key in (
            "trigger", "use_when", "not_when", "preconditions", "steps",
            "forbidden_tools", "expected_result", "failure_modes", "evidence",
            "confidence",
        )
    )


def refuse_playbook(rendered: str, name: str, tool_names: frozenset[str] | None) -> str | None:
    """Why a rendered playbook may not be written, or None. A plain skill is
    never refused here."""
    from tesseract.brain.playbook_contract import gaps_for_skill
    from tesseract.kernel.tools._path_door import refuse_paths
    from tesseract.paths import home_dir

    tmp_root = Path(tempfile.mkdtemp())
    folder = tmp_root / name
    folder.mkdir(parents=True, exist_ok=True)
    try:
        (folder / SKILL_FILENAME).write_text(rendered, encoding="utf-8")
        entry = load_skill_folder(folder)
    finally:
        try:
            (folder / SKILL_FILENAME).unlink(missing_ok=True)
            folder.rmdir()
            tmp_root.rmdir()
        except OSError:
            pass
    if entry is None or not entry.is_playbook:
        return None

    blocking = [g for g in gaps_for_skill(entry, tool_names=tool_names) if g.blocking]
    if blocking:
        return "Refused, this playbook cannot run: " + "; ".join(
            f"{g.field} {g.detail}" for g in blocking
        )

    # Every field a person or a model wrote, the body included: the body is
    # what the assistant reads and follows, so a path there is the one that
    # matters most, and the description is what every prompt shows. The
    # judgment is `_path_door`'s, one module that knows every path shape and
    # judges by the string rather than by the host.
    texts = [entry.description, entry.trigger, entry.use_when, entry.not_when, entry.expected_result]
    texts += list(entry.preconditions) + list(entry.failure_modes)
    texts += [step.do for step in entry.steps]
    texts.append(_FRONTMATTER_RE.sub("", rendered, count=1))
    return refuse_paths(texts, home_dir())


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
    rendered = render_skill_markdown(inp)
    return (
        f"Proposer: {inp.proposer}\n"
        f"Rationale: {inp.rationale}\n\n"
        f"--- Rendered SKILL.md ---\n\n{rendered}"
    )
