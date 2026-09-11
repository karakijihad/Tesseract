"""task_work: take a task up in this turn, so the turn and the task name each other.

A task the operator accepted sits on the agenda until a conversation takes it
up. This is how: the turn's record gains the task's id, the task gains the
turn's, and the task is `running`. Across restarts the join is what lets the
next boot say which task a stopped turn was working and keep the task while it
closes the turn.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.turns.tasks import (
    NotTakeable,
    last_attempt_was_interrupted,
    take_up,
)

logger = logging.getLogger(__name__)

#: What a resumed task is told when nothing on disk can say what it was doing.
#:
#: The careful answer, and it is now the FALLBACK rather than the rule. It was
#: the rule while no record could tell a call that may be repeated from one
#: that may not, and asking the operator about every one of them was the only
#: safe thing left. `recovery/resume.py` reads that record when there is one,
#: and a call the tool itself declared repeatable is repeated without spending
#: the operator's attention on it.
RESUME_NOTICE = (
    "The last turn on this stopped when the app restarted, and there is no "
    "record left of what it was doing. Anything it did with an effect may or "
    "may not have landed: say what the next step is and let the operator "
    "confirm it before you act."
)


class TaskWorkInput(BaseModel):
    item_id: str = Field(description="The task's id, as `task_propose` returned it.")


class TaskWorkTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "owed-work"
    summary: ClassVar[str] = "Take an accepted task up in this turn and read what it owes."
    use_when: ClassVar[str] = (
        "Use at the start of any turn spent on a task the operator accepted, "
        "including one you are picking back up after a restart. It marks the "
        "task running, ties this turn to it, and gives back the goal, what "
        "would count as done, and what earlier turns found."
    )
    not_when: ClassVar[str] = (
        "to create a task, which is `task_propose`; to finish one, which is "
        "`task_close`; for a session checklist, which is `tasks_set`."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "idempotent"

    def __init__(self, store: AgendaStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "task_work"

    @property
    def input_schema(self) -> type[BaseModel]:
        return TaskWorkInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, TaskWorkInput)
            else TaskWorkInput(**tool_input.model_dump())
        )
        item_id = inp.item_id.strip()
        item = self._store.get(item_id) if item_id else None
        if item is None:
            return ToolResult(
                output=f"There is no task {item_id!r}.",
                is_error=True,
                caller_error=True,
            )
        resumed = last_attempt_was_interrupted(item)
        # Read before `take_up`, which appends THIS turn's id. Afterwards the
        # index of the interrupted turn depends on whether the caller had a
        # turn at all, and a fallback that reads the wrong turn is a brief
        # about somebody else's calls.
        stopped_in = item.turn_ids[-1] if item.turn_ids else ""
        try:
            take_up(item, turn_id=context.turn_id, bind=context.bind_task, store=self._store)
        except NotTakeable as exc:
            return ToolResult(output=str(exc), is_error=True, caller_error=True)
        except (OSError, ValueError) as exc:
            logger.exception("task_work: could not take up %s", item_id)
            return ToolResult(
                output=f"{item_id} could not be marked running, so nothing is tied to this turn: {exc}",
                is_error=True,
            )

        lines = [
            f"{item.id} is running, and this turn is on its record.",
            f"Goal: {item.goal}",
            f"Done when: {item.success_criteria}",
        ]
        if item.verification:
            lines.append(f"Found so far: {item.verification}")
        if item.attempts:
            lines.append(f"Earlier turns on it: {len(item.attempts)}")
        if not context.turn_id:
            lines.append("No turn is being recorded here, so the task names no turn for this work.")
        if resumed:
            # The record first, and the careful sentence only when there is
            # none. Which of the two a turn gets is decided by whether the
            # runtime can actually say what happened, never by how cautious the
            # copy sounds.
            from tesseract.orchestrator.recovery import resume as resume_reader

            lines.append(
                resume_reader.for_task(
                    current_checkpoint=item.current_checkpoint,
                    last_turn_id=stopped_in,
                )
                or RESUME_NOTICE
            )
        return ToolResult(
            output="\n".join(lines),
            receipt=Receipt(
                kind="record",
                id=item.id,
                locator=str(self._store.path_for(item.id) or ""),
            ),
            metadata={"item_id": item.id, "turn_id": context.turn_id, "resumed": resumed},
        )
