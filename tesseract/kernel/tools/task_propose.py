"""task_propose: the one door through which a request becomes a task.

A task is an agenda item that is owed until it is verified. There is no other
way to make one: nothing promotes a turn by size or by heuristic, and the
operator's tap on this tool's gate IS the acceptance. Declined, nothing is
written. Approved, the item is on the agenda with what it owes written down,
and it survives the turn, the process and the context window because the
agenda already does.

The kernel does not dispatch a task to a background worker. A task is worked in
the conversation that proposed it, across as many turns as it takes, and
`kernel.py::_select_and_dispatch` leaves this source alone so the work is not
done twice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)

logger = logging.getLogger(__name__)


#: Said back when a task will close on the assistant's own sentence. The
#: closing tool says the same thing at the other end; this says it while there
#: is still time to name a project instead.
_ON_YOUR_WORD = "Nothing will check it, so it closes on your word."


def _verify_contract(project_id: str) -> str:
    """The project's declared checks as they stand right now, for the record.

    Fixed onto the task here, beside `success_criteria`, because the two are
    one contract: what would count as done, written and executable. Read at
    CLOSE instead and the meaning of `gate` can change after the work began.
    Best effort, like everything else that reads the registry here: a snapshot
    that cannot be taken leaves the field empty, which reads as "not recorded"
    and never as "no checks".
    """
    if not project_id:
        return ""
    from tesseract.orchestrator.projects.store import ProjectStore

    try:
        project = ProjectStore().get(project_id)
    except Exception:  # noqa: BLE001 - the task matters more than the snapshot
        logger.warning("task_propose: could not snapshot %s's checks", project_id)
        return ""
    return project.verify.as_contract() if project is not None else ""


def _resolve_project_id(requested: str) -> tuple[str, str]:
    """The project a new task belongs to, and what that means for its close.

    A NAMED project wins whatever it declares. Naming one is a decision and
    this is not the place to overrule it; a name that matches nothing is still
    refused rather than dropped.

    An UNNAMED task takes the active project ONLY when that project declares a
    check. The default is the one case nobody chose, and the active project is
    often an umbrella that declares nothing: filing work under it attaches a
    name, still closes on the assistant's own sentence, and both learners then
    throw the record away. Saying "no project" is the same close and an honest
    record of it, and it does not put unrelated work under a project's name.

    The second element is what to tell the caller, because a task that will
    close on a sentence should never be made silently.
    """
    from tesseract.orchestrator.projects.store import ProjectStore

    store = ProjectStore()
    try:
        if requested:
            found = store.get(requested)
        else:
            active = store.active()
            if active is None:
                return "", f"It belongs to no project. {_ON_YOUR_WORD}"
            if active.verify.is_empty():
                return "", (
                    f"It belongs to no project: {active.name} is the one open "
                    f"and it declares no checks. {_ON_YOUR_WORD} Name a "
                    f"project that declares checks to have them decide."
                )
            return active.id, f"{active.name}'s own checks decide when it is done."
    except Exception as exc:
        # A registry that cannot be read must not stop a task from being
        # made: a task with no project closes on the word and says so. A
        # NAMED project is the one case to refuse, because the name cannot
        # be checked and filing it unchecked is the silent drop above.
        logger.warning("task_propose: the project registry could not be read: %s", exc)
        if requested:
            raise LookupError(
                f"The project registry could not be read, so {requested!r} "
                f"cannot be checked. Leave project_id empty to make the task "
                f"without a project, or fix the registry first."
            ) from exc
        return "", f"It belongs to no project. {_ON_YOUR_WORD}"
    if found is None:
        raise LookupError(
            f"There is no project {requested!r}. project_list names the "
            f"ones that exist; leave project_id empty for the active one."
        )
    if found.verify.is_empty():
        return requested, f"{found.name} declares no checks. {_ON_YOUR_WORD}"
    return requested, f"{found.name}'s own checks decide when it is done."


def _why_not_unattended(
    context: ToolContext, project_id: str, criteria: str, estimate: float | None
) -> str:
    """Why this proposal may not be made with nobody watching, or `""`.

    **The gate is the session kind and nothing else.** A posture cannot draw
    this line: `task_propose` is `ask` shipped and `auto` under `free`, and
    `free` is the mode the unattended path exists for, so the posture that
    protects the operator's install is exactly the one that is relaxed where
    this matters. `ask_fn is None` cannot draw it either, for the same reason.

    In the operator's own chat nothing here applies, and that is deliberate
    rather than a gap. Every one of these refusals is a sentence they could
    answer: price it, check it by hand, spend past it today. Unattended there
    is nobody to answer, so the record has to.

    Fails CLOSED on a registry it cannot read, which is the opposite of what
    `_resolve_project_id` does one line above and is right in both places: an
    unreadable registry there still lets a person make a task with no project,
    and here it means the ceiling cannot be seen at all.

    **What today has already committed is asked for here, not inside the
    gate.** The gate is pure over what it is handed, and the answer needs the
    agenda store. Without it three steps proposed in one turn each fitted the
    room left, because the ledger has recorded none of them yet and none of
    them knew about the other two.
    """
    if context.session_kind != "autonomy":
        return ""
    from tesseract.orchestrator.autonomy.morning import (
        committed_today,
        spent_today_by_project,
        why_a_step_is_refused,
    )
    from tesseract.orchestrator.projects.store import ProjectStore

    try:
        project = ProjectStore().get(project_id) if project_id else None
    except Exception:  # noqa: BLE001 - a ceiling nobody can read is not a ceiling
        logger.warning("task_propose: the registry could not be read for a step")
        return "the project registry could not be read, so no budget can be checked"
    return why_a_step_is_refused(
        project,
        criteria,
        estimate,
        spent_today_by_project(),
        committed_today(project_id),
    )


class TaskProposeInput(BaseModel):
    goal: str = Field(
        min_length=1,
        max_length=500,
        description="What the operator asked for, in one sentence.",
    )
    success_criteria: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "What observable evidence would show it is done: a file that "
            "exists, a test that passes, a message that arrived. Not a "
            "feeling and not a sentence you would write at the end."
        ),
    )
    rationale: str = Field(
        default="",
        max_length=2000,
        description="Why this is worth a task rather than an answer, if it needs saying.",
    )
    project_id: str = Field(
        default="",
        max_length=120,
        description=(
            "The project this task belongs to, by its id from project_list. "
            "Leave empty to take the project that is open, which happens only "
            "when it declares checks. A task with a project that declares "
            "checks is closed by them; every other task closes on your word."
        ),
    )
    estimated_cost_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "What you expect this step to cost in dollars. It is checked "
            "against what the project has left to spend today, and a step "
            "nobody is watching is refused if it does not fit. It is kept on "
            "the record either way, and every step still waiting is subtracted "
            "from what that project may spend today, so a number given here in "
            "conversation still counts. Leave it out if you cannot say."
        ),
    )


class TaskProposeTool(Tool):
    # NOT a default to be tuned. The gate's prompt is where the operator
    # accepts the task: the model proposes, the human tap approves, and on a
    # channel that tap is the same inline keyboard every gated call raises.
    # Set this to auto and the assistant can give itself work.
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "owed-work"
    summary: ClassVar[str] = "Turn a request into a task that is owed until it is verified."
    use_when: ClassVar[str] = (
        "Use when the operator has asked for something that will take more than "
        "this turn, or that must still be done if the app restarts or the "
        "conversation is lost. Say what would count as done in terms you can "
        "check. The operator confirms it before anything is written, so their "
        "tap is the acceptance."
    )
    not_when: ClassVar[str] = (
        "a question you can answer now, or a step inside work already on a "
        "task. A checklist for this session only is `tasks_set`; a comment on "
        "an existing agenda item is `agenda_comment`."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "idempotent"

    def __init__(self, store: AgendaStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "task_propose"

    @property
    def input_schema(self) -> type[BaseModel]:
        return TaskProposeInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def ask_reason(self, validated: Any) -> str:
        """What the operator is agreeing to, readable on a phone: the goal and
        what would end it, never an id."""
        inp = validated if isinstance(validated, TaskProposeInput) else None
        if inp is None:
            return "make this a task"
        return f"make this a task: {inp.goal.strip()} It is done when: {inp.success_criteria.strip()}"

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, TaskProposeInput)
            else TaskProposeInput(**tool_input.model_dump())
        )
        goal = inp.goal.strip()
        criteria = inp.success_criteria.strip()
        if not goal or not criteria:
            return ToolResult(
                output=(
                    "A task needs a goal and what would count as done. Say both, "
                    "or answer in the conversation instead."
                ),
                is_error=True,
                caller_error=True,
            )

        try:
            project_id, whats_owed = _resolve_project_id(inp.project_id.strip())
        except LookupError as exc:
            return ToolResult(output=str(exc), is_error=True, caller_error=True)

        refusal = _why_not_unattended(context, project_id, criteria, inp.estimated_cost_usd)
        if refusal:
            return ToolResult(
                output=f"Not proposed: {refusal}.",
                is_error=True,
                caller_error=True,
                metadata={"refused_unattended": True},
            )

        existing = self._store.find_dedupe(goal, AgendaSource.TASK)
        if existing is not None:
            return ToolResult(
                output=(
                    f"That is already a task: {existing.id} is "
                    f"{existing.status.value.replace('_', ' ')}. Work that one "
                    f"rather than opening a second."
                ),
                is_error=True,
                caller_error=True,
                metadata={"item_id": existing.id, "deduped": True},
            )

        now = datetime.now(timezone.utc)
        item = AgendaItem(
            id=mint_agenda_id(goal, now=now),
            created_at=now,
            updated_at=now,
            source=AgendaSource.TASK,
            goal=goal,
            rationale=inp.rationale.strip(),
            risk_class=RiskClass.PROPOSE,
            status=AgendaStatus.PROPOSED,
            success_criteria=criteria,
            project_id=project_id,
            verify_snapshot=_verify_contract(project_id),
            estimated_cost_usd=inp.estimated_cost_usd,
        )
        try:
            self._store.add(item, by="operator", reason="accepted at the gate")
        except ValueError as exc:
            return ToolResult(
                output=f"Not made a task: {exc}",
                is_error=True,
                caller_error=True,
            )
        except OSError:
            logger.exception("task_propose: the agenda could not be written")
            return ToolResult(
                output=(
                    "The task could not be written to the agenda, so nothing is "
                    "owed yet. Try again, or do the work in this conversation."
                ),
                is_error=True,
            )
        return ToolResult(
            output=(
                f"{item.id} is a task now. It is done when: {criteria} "
                f"{whats_owed} "
                f"The record keeps what is owed across restarts; work it here, "
                f"and record the evidence before closing it."
            ),
            receipt=Receipt(
                kind="record",
                id=item.id,
                locator=str(self._store.path_for(item.id) or ""),
            ),
            metadata={"item_id": item.id, "deduped": False},
        )
