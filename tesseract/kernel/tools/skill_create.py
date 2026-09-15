"""skill_create tool — draft a new markdown skill under workspace/skills/.

Mirror of `agent_create`. A skill is for a procedure that will come again,
never a one-off task and never general advice: the assistant (or a delegate)
writes down the shape of problem it answers, the exact situation that calls
for it, the steps and the tool each one uses, and what done looks like
(`brain/skills.py::CONTRACT_KEYS`). A field left unset is a gap
`playbook_contract.gaps_for_skill` reports, never a reason the write is
refused: a file that declares none of the contract still loads. Attended
sessions get the posture `permissions.yaml` sets for the mode (`ask` where
the operator keeps the decision, `auto` where they gave it away); unattended,
the executor's quarantine-write carve-out (`headless_quarantine_write`
ClassVar, honored by `permissions/decide.py` from the CLASS only) lets the
call proceed because the only write target is the uninvokable quarantine
below.

A skill is live and carried the moment it goes through: on its own where this
tool's posture resolves to auto, or on its approved `skill_approval` card
otherwise. Nothing waits for a second use to matter, so use it on the task in
front of you as soon as it lands.

`origin` says who is behind the draft, and is carried onto the proposal card:
`agent` (the default) is the assistant acting on its own judgement that
something just worked and will come again; `operator` is the assistant
drafting because the operator asked for it in words; `observer` is the
assistant agreeing with a skill nudge the observer raised
(`agents/observer.md`'s third job) and drafting it, which OPERATING.md tells
it to do with this same tool rather than on the observer's say-so alone.

Every write goes through the one door (`brain/skill_door.py::create_skill`):
it refuses before the write when the playbook cannot run (a step naming a
tool the runtime lacks or one the playbook forbids), when any field names a
credential-bearing path or a path outside the home tree, when the name is
already active, pending or rejected, or when the proposed steps already match
an existing skill's exactly. The door also writes the draft, files the
proposal card, and promotes it where the posture allows — this tool only
builds the draft and translates the door's answer into a result.

Quarantine: the skill is written to `workspace/skills/pending/<name>/SKILL.md`,
NOT directly to the active tree. `brain/skills.py::load_skills` skips
`pending/`, so a drafted skill never appears in the prompt manifest until the
operator approves its `skill_approval` card.

Writes: workspace/skills/pending/<name>/SKILL.md + a skill_approval event.
Never edits or deletes existing skills.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.brain.skill_door import (
    DraftStep,
    SkillDraft,
    create_skill,
    refuse_playbook,
)
from tesseract.brain.skill_door import render_skill_markdown as _door_render_skill_markdown
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.workspace_events import EventStore

logger = logging.getLogger(__name__)

# Agent Skills standard: name ≤ 64 chars, slug-style for the folder.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

# `task_done` is not offered here: it names a scheduled job's own draft, and
# the tool call this input belongs to is always either a live conversation or
# a headless agent turn, never the job.
ToolOrigin = Literal["agent", "operator", "observer"]


class PlaybookStep(BaseModel):
    do: str = Field(description="What this step does.")
    tool: str = Field(default="", description="The exact tool it calls. Leave it empty for a step that calls none.")


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
        description=(
            "The SKILL.md body: the markdown playbook the assistant reads on "
            "demand. Keep it short; the `steps` carry the procedure, not an essay."
        )
    )
    rationale: str = Field(
        description="Why this skill is needed. The operator reads this when approving it."
    )
    origin: ToolOrigin = Field(
        default="agent",
        description=(
            "Where this draft came from: `agent` if you decided on your own "
            "that something just worked and will come again; `operator` if "
            "the operator asked for it in words; `observer` if you are "
            "acting on a skill nudge from the observer. Recorded on the "
            "proposal card."
        ),
    )
    proposer: Literal["entity", "claude", "codex", "user"] = Field(
        default="entity",
        description="Who is proposing this skill. Recorded for review.",
    )
    version: str = Field(
        default="0.1",
        description=(
            "Ignored: a new skill is version 1 and the runtime writes it."
        ),
    )
    license: str | None = Field(default=None)
    allowed_tools: list[str] | None = Field(
        default=None,
        description=(
            "Agent-Skills `allowed-tools`. List exactly the tools the "
            "`steps` use, no more, no less."
        ),
    )
    # The procedure contract. A field left unset here is a gap
    # `playbook_contract` reports, not a reason the write is refused.
    trigger: str | None = Field(
        default=None,
        description=(
            "The specific situation this answers, in the operator's words, e.g. "
            "'a brief on a topic, sent to the phone'. Not a general category."
        ),
    )
    use_when: str | None = Field(
        default=None,
        description="The specific situation to reach for this skill, not a general category.",
    )
    not_when: str | None = Field(
        default=None,
        description=(
            "Name the nearest sibling skill this must not be confused with, "
            "and what makes this one the wrong pick for it."
        ),
    )
    preconditions: list[str] | None = Field(
        default=None, description="What must be true before step one. May be empty.",
    )
    steps: list[PlaybookStep] | None = Field(
        default=None,
        description=(
            "Ordered steps. Each says what to do and names the exact tool it "
            "calls, or leaves `tool` empty for a step that calls none."
        ),
    )
    forbidden_tools: list[str] | None = Field(
        default=None,
        description="Tools this playbook must never use, only for a real hazard. Leave empty otherwise.",
    )
    expected_result: str | None = Field(
        default=None,
        description="What done looks like, worded so a run can be checked against it.",
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
    summary: ClassVar[str] = "Write a new skill; it is live and carried once made or approved."
    use_when: ClassVar[str] = (
        "Use for a procedure you expect to need again, never a one-off task "
        "and never general advice. Give `trigger` and `use_when` naming the "
        "exact situation that calls for it, `not_when` naming the nearest "
        "sibling skill this must not be confused with, `steps` each saying "
        "what to do and naming the exact tool it calls (leave `tool` empty "
        "for a step that calls none), `allowed_tools` listing exactly the "
        "tools the steps use, and a checkable `expected_result`. Also use "
        "this to act on an observer skill nudge, with `origin: observer`. It "
        "goes live and carried as soon as it is made or approved, so use it "
        "on the task in front of you."
    )
    not_when: ClassVar[str] = (
        "for a one-off task, or advice with no procedure to repeat; to "
        "improve an existing active skill, use `skill_refine` instead."
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

        # --- Validation the door does not own: shape, not policy ---
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

        result = await create_skill(
            _draft_from_input(inp),
            skills_dir=self._skills_dir,
            origin=inp.origin,
            attended=context.ask_fn is not None,
            event_store=self._event_store,
            app_provider=self._app_provider,
            tool_names=self._tool_names(),
            registry=self._registry_provider(),
            card_title=f"Skill proposal: {inp.name}",
            card_summary=inp.rationale,
            card_extra={"proposer": inp.proposer, "session_id": context.session_id},
        )

        if result.status.startswith("refused"):
            return ToolResult(
                output=result.reason,
                is_error=True,
                caller_error=result.status not in ("refused_write", "refused_card"),
            )

        if result.status == "created_active":
            return ToolResult(
                output=(
                    f"Created and activated skill: {result.name}\n"
                    f"File: {result.folder / 'SKILL.md'}\n\n"
                    "Promotion needs no approval in this mode, so it is live "
                    "now, carried, and listed in your prompt from the next "
                    "turn." + result.card_note
                ),
                receipt=Receipt(kind="record", id=result.name, locator=str(result.folder / "SKILL.md")),
            )

        # "created_pending" (and "supported", which a tool call never
        # actually reaches — that outcome is `task_done`-only).
        return ToolResult(
            output=(
                f"Created skill (pending promotion): {result.name}\n"
                f"File: {result.folder / 'SKILL.md'}\n\n"
                "The skill is quarantined: it does not appear in the prompt "
                "manifest until the operator approves its Workspace "
                "proposal card." + result.card_note
            ),
            receipt=Receipt(kind="record", id=result.name, locator=str(result.folder / "SKILL.md")),
        )


# ─── Helpers ─────────────────────────────────────────────


def _draft_from_input(inp: SkillCreateInput) -> SkillDraft:
    """The door's own draft shape, off this tool's pydantic input."""
    return SkillDraft(
        name=inp.name,
        description=inp.description,
        instructions=inp.instructions,
        rationale=inp.rationale,
        proposer=inp.proposer,
        license=inp.license,
        allowed_tools=tuple(inp.allowed_tools or []),
        trigger=inp.trigger or "",
        use_when=inp.use_when or "",
        not_when=inp.not_when or "",
        preconditions=tuple(inp.preconditions or []),
        steps=tuple(DraftStep(do=s.do, tool=s.tool) for s in (inp.steps or [])),
        forbidden_tools=tuple(inp.forbidden_tools or []),
        expected_result=inp.expected_result or "",
        failure_modes=tuple(inp.failure_modes or []),
        evidence=tuple(inp.evidence or []),
        confidence=inp.confidence,
    )


def render_skill_markdown(inp: SkillCreateInput) -> str:
    """Render the full SKILL.md content, via the door. Kept here — rather
    than only in `skill_door`, which works over its own `SkillDraft` — because
    this tool's callers hold a `SkillCreateInput`."""
    return _door_render_skill_markdown(_draft_from_input(inp))


def description_for_approval(inp: SkillCreateInput) -> str:
    """Return the input_summary for the permission engine approval prompt."""
    rendered = render_skill_markdown(inp)
    return (
        f"Proposer: {inp.proposer}\n"
        f"Rationale: {inp.rationale}\n\n"
        f"--- Rendered SKILL.md ---\n\n{rendered}"
    )
