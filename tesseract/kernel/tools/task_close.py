"""task_close: end a task with the evidence, and the project's own checks decide.

`done` is a claim with evidence behind it, and `failed` is a reason. When the
task belongs to a project that declares verify steps, those steps are the
evidence: the gate runs them through the same permission path as any other
command, and what it found is what lands in `verification`. The assistant's
closing sentence is recorded as the claim, never as the proof. A task with no
project, or a project with nothing declared, closes on the sentence and says
so, so a reader can tell the two apart.

**A declared failure runs the gate too.** It used to be the one ungated exit:
say `failed` and no command ever ran, so every failure reached the learners as
the assistant's own word and only gated successes carried evidence. That is
survivorship bias in the thing the runtime learns from. Now a failing step
corroborates the failure and is recorded as `gate`; a passing gate leaves the
claim standing but labelled `model`, because the gate reports on the PROJECT
and only the assistant can say this task's own criteria went unmet. A declared
failure is never turned into a `done`.

**And the checks are the ones that were promised.** `task_propose` writes the
project's declared commands onto the task when it is accepted, beside
`success_criteria`, because they are the executable half of the same contract.
Read from the registry at close instead and what `gate` MEANS can change after
the work began, which matters here because `project_link` resolves to auto in
the mode this runs in: the assistant can rewrite a project's checks itself, and
the cheapest route to a clean record would be to weaken the check rather than
do the work. Changed in between, the gate still runs and its output is still
kept, but the close is labelled `model`: nobody can say the proof is the one
that was owed, so it is not counted as one.

Under an operator-attended posture the operator reads all of it before it
lands. Under full auto nobody reads it, which is why the gate has to.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, ClassVar, Literal

from pydantic import BaseModel, Field

from tesseract.kernel.tokenjuice.reducers import cap_chars
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    UnexplainedFailure,
    UnverifiedDone,
)
from tesseract.orchestrator.projects.models import Project
from tesseract.orchestrator.verify.models import GateResult

logger = logging.getLogger(__name__)

# `AgendaItem.verification` is capped at 4000; `cap_chars` appends a note past
# `n`, so the cap it is given leaves room for that note.
_VERIFICATION_CHARS = 3800

GateRunner = Callable[[Project, ToolContext], Awaitable[GateResult]]


async def _run_project_gate(project: Project, context: ToolContext) -> GateResult:
    """The project's verify steps under the turn's own permission context.

    The executor is the policy one: a verify command is a `bash` call the
    turn is making, with the turn's `ask_fn` and policy, so `free` runs it
    and `max` asks. The gate never decides what may run.
    """
    from tesseract.orchestrator.verify.gate import run_gate
    from tesseract.orchestrator.verify.policy_executor import PolicyExecutor

    executor = PolicyExecutor(
        policy=context.policy,
        ask_fn=context.ask_fn,
        session_id=context.session_id,
        caller_principal=context.caller_principal,
        # So the gate's own bash calls land on the same record as the turn's,
        # rather than in the stream shared by everything with no conversation.
        chat_id=context.chat_id,
        run_id=context.turn_id or context.run_id,
    )
    return await run_gate(project.verify, cwd=project.root, executor=executor)


def _project_for(item: AgendaItem) -> Project | None:
    if not item.project_id:
        return None
    from tesseract.orchestrator.projects.store import ProjectStore

    try:
        return ProjectStore().get(item.project_id)
    except Exception:
        logger.exception("task_close: could not read the project registry")
        return None


class TaskCloseInput(BaseModel):
    item_id: str = Field(description="The task's id.")
    outcome: Literal["done", "failed"] = Field(
        description="`done` when the evidence meets what was declared; `failed` when it cannot be met and there is no decision left to ask for."
    )
    evidence: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "For `done`, the observable evidence that meets the criteria: what "
            "was checked, where, what it showed. For `failed`, what stopped "
            "it and what was tried. On a project with verify steps this is "
            "your claim; the steps are what lands as the proof."
        ),
    )


class TaskCloseTool(Tool):
    # The class floor is the attended posture; `permissions.yaml` decides per
    # mode. What holds under full auto is the gate below, not this value.
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "owed-work"
    summary: ClassVar[str] = "Close a task as done with its evidence, or as failed with the reason."
    use_when: ClassVar[str] = (
        "Use when the evidence meets what the task declared would count as "
        "done, or when it cannot be met and there is no decision left to ask "
        "the operator for. Give the evidence itself, not a summary of your "
        "work. On a project with verify steps the steps run and decide; a "
        "failing step closes the task as failed whatever you said."
    )
    not_when: ClassVar[str] = (
        "the task is waiting on the operator or blocked: leave it, the panel "
        "carries it. To pause work without ending it, just stop; the task "
        "stays running for the next turn."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "idempotent"

    def __init__(self, store: AgendaStore, *, gate: GateRunner | None = None) -> None:
        self._store = store
        self._gate = gate or _run_project_gate

    @property
    def name(self) -> str:
        return "task_close"

    @property
    def input_schema(self) -> type[BaseModel]:
        return TaskCloseInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def ask_reason(self, validated: Any) -> str:
        inp = validated if isinstance(validated, TaskCloseInput) else None
        if inp is None:
            return "close a task"
        goal = ""
        try:
            item = self._store.get(inp.item_id.strip())
            goal = item.goal if item is not None else ""
        except Exception:
            logger.exception("task_close: could not read the task to describe it")
        what = goal or inp.item_id
        return f"close as {inp.outcome}: {what} Evidence: {inp.evidence.strip()}"

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, TaskCloseInput)
            else TaskCloseInput(**tool_input.model_dump())
        )
        item_id = inp.item_id.strip()
        evidence = inp.evidence.strip()
        item = self._store.get(item_id) if item_id else None
        if item is None:
            return ToolResult(
                output=f"There is no task {item_id!r}.",
                is_error=True,
                caller_error=True,
            )
        if item.source is not AgendaSource.TASK:
            return ToolResult(
                output=f"{item.id} is autonomy's own item, not a task, and closes on its own.",
                is_error=True,
                caller_error=True,
            )
        if item.is_terminal():
            return ToolResult(
                output=f"{item.id} is already {item.status.value.replace('_', ' ')}.",
                is_error=True,
                caller_error=True,
            )
        where = f"in turn {context.turn_id}" if context.turn_id else "outside a recorded turn"

        project = _project_for(item)
        if project is None or project.verify.is_empty():
            # No checks either way, so a declared failure and a declared done
            # are both the assistant's word and both say so.
            if inp.outcome == "failed":
                return self._close(
                    context,
                    item, AgendaStatus.FAILED, verification=evidence, by="model",
                    reason=evidence[:500],
                    said=f"{item.id} is failed. The reason is on its record.",
                )
            return self._close(
                context,
                item, AgendaStatus.DONE, verification=evidence, by="model",
                reason=f"claimed {where}: {evidence[:200]}",
                said=f"{item.id} is done on your word: it has no project checks. The evidence is on its record.",
            )

        try:
            result = await self._gate(project, context)
        except Exception as exc:
            logger.exception("task_close: the gate could not run for %s", item_id)
            return ToolResult(
                output=f"{item.id} stays open: its checks could not run ({exc}).",
                is_error=True,
            )
        from tesseract.orchestrator.verify.render import render_gate

        # The checks that just ran against the ones promised when the task was
        # accepted. Changed in between and the label is withheld: the gate ran
        # and its output is kept, but nobody can say the proof is the one that
        # was owed, so it is not counted as one. Empty means a record written
        # before the snapshot existed, which cannot be judged and is left alone.
        drifted = bool(item.verify_snapshot) and item.verify_snapshot != project.verify.as_contract()
        decided: Literal["gate", "model"] = "model" if drifted else "gate"
        note = (
            "\n\nThe project's checks changed after this task was accepted, so "
            "what ran is not what was promised and this does not count as "
            "checked.\nPromised:\n" + item.verify_snapshot
        ) if drifted else ""

        rendered = cap_chars(render_gate(result) + note, n=_VERIFICATION_CHARS)
        if result.failed_steps:
            # The one place a step's failure is the finding whichever way the
            # close was claimed: it corroborates a declared failure, and it
            # overrules a declared done.
            first = result.failed_steps[0]
            return self._close(
                context,
                item, AgendaStatus.FAILED, verification=rendered, by=decided,
                reason=f"{first.name} failed {where}; claimed: {evidence[:200]}",
                said=(
                    f"{item.id} is failed: {first.name} did not pass, whatever the "
                    f"evidence said. The step's output is on its record."
                ),
            )
        if result.vacuous:
            blocked = "; ".join(
                f"{s.name}: {s.skipped_reason}" for s in result.blocked_steps
            ) or "no step ran"
            if inp.outcome == "failed":
                # A declared failure is not held open for want of a check.
                # Nothing ran, so nothing corroborates it and it closes on the
                # word, which is what a task with no checks does anyway.
                return self._close(
                    context,
                    item, AgendaStatus.FAILED, verification=f"{evidence}\n\n{rendered}",
                    by="model", reason=evidence[:500],
                    said=(
                        f"{item.id} is failed on your word: none of "
                        f"{project.name}'s checks ran ({blocked})."
                    ),
                )
            return ToolResult(
                output=(
                    f"{item.id} stays open: none of {project.name}'s checks ran "
                    f"({blocked}). Nothing was proved either way."
                ),
                is_error=True,
            )
        if inp.outcome == "failed":
            # The checks pass and the assistant says it could not do the work.
            # The claim stands, because they answer different questions: the
            # gate reports on the PROJECT, and only the assistant is in a
            # position to say this task's own criteria went unmet. But the
            # failure is its word and not the gate's, so `model` is the honest
            # label even though a gate ran, and the passing output is kept
            # beside the claim so a reader sees both.
            return self._close(
                context,
                item, AgendaStatus.FAILED, verification=f"{evidence}\n\n{rendered}",
                by="model", reason=f"claimed failed {where}: {evidence[:200]}",
                said=(
                    f"{item.id} is failed on your word. {project.name}'s checks "
                    f"passed, which says the project is healthy and not that "
                    f"the task was done; both are on its record."
                ),
            )
        return self._close(
            context,
            item, AgendaStatus.DONE, verification=rendered, by=decided,
            reason=f"checks passed {where}; claimed: {evidence[:200]}",
            said=(
                f"{item.id} is done: {project.name}'s checks passed "
                f"({', '.join(s.name for s in result.passed_steps)}). "
                f"Their output is on its record."
            ),
        )

    def _close(
        self,
        context: ToolContext,
        item: AgendaItem,
        status: AgendaStatus,
        *,
        verification: str,
        by: Literal["gate", "model"],
        reason: str,
        said: str,
    ) -> ToolResult:
        item.verification = verification
        item.verification_by = by
        try:
            self._store.transition(item, status, reason=reason)
        except (UnverifiedDone, UnexplainedFailure) as exc:
            return ToolResult(output=str(exc), is_error=True, caller_error=True)
        except (OSError, ValueError) as exc:
            logger.exception("task_close: could not close %s", item.id)
            return ToolResult(output=f"{item.id} was not closed: {exc}", is_error=True)
        outcome = "done" if status is AgendaStatus.DONE else "failed"
        # After the transition, never before: the turn's record says what
        # happened, and a close that was refused did not happen. `None` off a
        # turn (the REPL, a test), and a record that cannot be written is the
        # record's problem, not the task's.
        if context.note_task_closed is not None:
            try:
                context.note_task_closed(outcome, by)
            except Exception:  # noqa: BLE001 - the task is closed either way
                logger.exception("task_close: could not put %s on the turn record", item.id)
        return ToolResult(
            output=said,
            receipt=Receipt(
                kind="record",
                id=item.id,
                locator=str(self._store.path_for(item.id) or ""),
            ),
            metadata={"item_id": item.id, "outcome": outcome, "verification_by": by},
        )
