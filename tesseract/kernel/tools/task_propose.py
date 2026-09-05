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
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)

logger = logging.getLogger(__name__)


def _resolve_project_id(requested: str) -> str:
    """The project a new task belongs to: the one named, else the active one,
    else none. A name that matches nothing is refused rather than dropped,
    because a task silently filed without its project would close on the
    assistant's word when the operator meant it to close on the checks."""
    from tesseract.orchestrator.projects.store import ProjectStore

    store = ProjectStore()
    try:
        if requested:
            found = store.get(requested)
        else:
            active = store.active()
            return active.id if active is not None else ""
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
        return ""
    if found is None:
        raise LookupError(
            f"There is no project {requested!r}. project_list names the "
            f"ones that exist; leave project_id empty for the active one."
        )
    return requested


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
            "Leave empty for the active project. A task with a project is "
            "closed by the project's own checks; one without closes on your word."
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
        del context
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
            )

        try:
            project_id = _resolve_project_id(inp.project_id.strip())
        except LookupError as exc:
            return ToolResult(output=str(exc), is_error=True)

        existing = self._store.find_dedupe(goal, AgendaSource.TASK)
        if existing is not None:
            return ToolResult(
                output=(
                    f"That is already a task: {existing.id} is "
                    f"{existing.status.value.replace('_', ' ')}. Work that one "
                    f"rather than opening a second."
                ),
                is_error=True,
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
        )
        try:
            self._store.add(item, by="operator", reason="accepted at the gate")
        except ValueError as exc:
            return ToolResult(output=f"Not made a task: {exc}", is_error=True)
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
                f"The record keeps what is owed across restarts; work it here, "
                f"and record the evidence before closing it."
            ),
            metadata={"item_id": item.id, "deduped": False},
        )
