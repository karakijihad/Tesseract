"""The row that makes the app alive when it opens, and the five ways it stops.

One row, once a day, on the engine's own catch-up shape: a cron at an hour
inside the operator's day, replayed once at the next open if the machine was
off when it came round. That is what "the first tick after open, and never
twice in a day" is built out of, and it is `consolidate`'s shape rather than a
second answer to the same question.

**Every stop before the model is a different sentence.** Four of them are
reached before anything is billed, and collapsing them would leave the operator
with one word for a machine that has reached its whole day's ceiling, one that
has no projects to work, one whose project budgets are all spent, and one that
could not read what it had spent. The last of those is the dangerous one: a
ledger nobody could read is not a full budget, and this row refuses rather than
guessing.

**It proposes and does not begin.** The `workday` row is what takes a step
forward, in this same conversation, on the wakes after this one; this turn ends
after the model has said what it thinks is worth doing.
`kernel.py::_select_and_dispatch` skips `AgendaSource.TASK`, so a step proposed
here is worked in a conversation and never handed to a worker.

**What came of it is counted, not parsed.** `morning.send_one_turn` counts the
`task_propose` calls on the turn, because that is the record. The prompt asks
for `NO_STEPS` so the model has a way to say so out loud, and the reply is
carried into the row's reason for the operator to read, but nothing keys off
the string.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

#: What the model calls to propose a step. Counted on the turn, so the row's
#: outcome is what happened rather than what the reply said about it.
_PROPOSE = "task_propose"

#: The mark this row's turns carry. See `workday._ORIGIN`: it has to exist in
#: `chat.RUNTIME_ORIGINS` and in the transcript's label map as well as here.
_ORIGIN = "morning"


def _an_idea_is_waiting() -> bool:
    """Whether an accepted idea is sitting unstarted, read from the record.

    Asked before the no-priced-project exit rather than inside the prompt,
    because that exit fires before a prompt is built at all. Blocking: it reads
    the workspace event log, so the caller threads it like every other read
    here.
    """
    from tesseract.orchestrator.autonomy.morning_prompt import (
        accepted_idea_lines,
        accepted_ideas,
    )

    return bool(accepted_idea_lines(accepted_ideas()))


def _steps_line(proposed: int) -> str:
    """What a successful morning shows the operator, as a sentence.

    One step is not "1 step(s)". This line is rendered on the Schedule view and
    in the run log, and the copy rule covers everything a person reads.
    """
    return f"it proposed {proposed} step" + ("" if proposed == 1 else "s")


class MorningJob(BaseJob):
    """Open the day: read what is owned, ask what is worth doing, stop.

    `uses_llm` is False on purpose even though this turn bills a model. That
    flag draws the per-row model dropdown in the Schedule view, and the model
    here is the conversation's — `chat_brain`, through the one funnel — so a
    per-row override would be an answer to a question this row does not ask.
    The manifest entry says `whatever the work it starts asks for` for the
    same reason.
    """

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            return await self._run(ctx, t0)
        except Exception as exc:  # noqa: BLE001 - contract: never raise
            log.exception("morning: the row failed")
            return self._result(
                ctx, t0, RunOutcome.FAILED, f"the morning could not run: {exc}"
            )

    async def _run(self, ctx: JobContext, t0: float) -> JobResult:
        from tesseract.orchestrator.autonomy.morning import (
            day_is_capped,
            room_left,
            spent_today_by_project,
            workable_projects,
        )
        from tesseract.orchestrator.autonomy.morning_prompt import build
        from tesseract.orchestrator.projects.store import ProjectStore

        # Before the project budgets, because this one is the machine's whole
        # day and they sit inside it. A morning that proposed against a ceiling
        # already reached would spend a model call working out that no step
        # fits. Threaded like every other read here: it parses a config file.
        capped = await asyncio.to_thread(day_is_capped, ctx.app)
        if capped:
            return self._result(ctx, t0, RunOutcome.REFUSED, capped)

        projects = await asyncio.to_thread(lambda: ProjectStore().list_projects())
        mine = workable_projects(projects)
        # **An accepted idea opens the day on its own**, so the no-priced-project
        # exit asks about it first. The operator most likely to have accepted an
        # idea for a NEW project is the one who has priced none yet, and
        # returning here left that idea in the inbox for good.
        waiting = await asyncio.to_thread(_an_idea_is_waiting)
        if not mine and not waiting:
            return self._result(
                ctx, t0, RunOutcome.SKIPPED_NO_WORK,
                "no project has a daily budget, so there is nothing it may "
                "work on its own. Price one and it will be picked up tomorrow",
            )

        # Blocking: it parses a file that only grows. Its own docstring asks
        # the caller to thread it so health and every open conversation stay
        # responsive while it reads.
        spent = await asyncio.to_thread(spent_today_by_project)
        if spent is None:
            return self._result(
                ctx, t0, RunOutcome.REFUSED,
                "today's spend could not be read, so there is no ceiling to "
                "work against. Nothing was proposed and nothing was spent",
            )

        room = {project.id: room_left(project, spent) for project in mine}
        prompt = await asyncio.to_thread(build, mine, room)
        if not prompt:
            return self._result(
                ctx, t0, RunOutcome.SKIPPED_NO_WORK,
                "every project it may work has spent its budget for today, and "
                "no idea is waiting to be started",
            )

        said, proposed = await self._one_turn(ctx.app, prompt)
        if proposed:
            return self._result(
                ctx, t0, RunOutcome.SUCCEEDED,
                "", proposed=proposed, said=said,
            )
        return self._result(
            ctx, t0, RunOutcome.SKIPPED_NO_WORK,
            f"it read the record and proposed nothing. {said}".strip(),
            said=said,
        )

    async def _one_turn(self, app: Any, prompt: str) -> tuple[str, int]:
        """Send the prompt through the day's own conversation.

        `morning.send_one_turn` is the seam and both rows of the day use it, so
        the morning and every wake after it are one conversation rather than
        two rows each opening their own.
        """
        from tesseract.orchestrator.autonomy.morning import send_one_turn

        return await send_one_turn(app, prompt, origin=_ORIGIN, counting=_PROPOSE)

    def _result(
        self,
        ctx: JobContext,
        t0: float,
        outcome: RunOutcome,
        reason: str,
        *,
        proposed: int = 0,
        said: str = "",
    ) -> JobResult:
        # `ok` is required and is then overwritten from `outcome` in
        # `__post_init__`. It is passed as what the outcome means so the two
        # never read as disagreeing at the call site either.
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=outcome is not RunOutcome.FAILED,
            detail=reason or _steps_line(proposed),
            outcome=outcome,
            outcome_reason=reason,
            payload={"proposed": proposed, "said": said},
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )
