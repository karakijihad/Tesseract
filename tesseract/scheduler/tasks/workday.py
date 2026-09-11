"""The row that carries the morning's decisions through the day, one step a wake.

The morning row decides and stops. This one wakes once an hour between the two
ends of the operator's day, takes the day's conversation forward by one turn,
and stops again. Nothing is proposed here and nothing is closed here: the turn
works a step and `task_close` decides whether it is done, against the project's
own checks.

**Every stop before the model is a different sentence**, the same rule the
morning row holds. A day with no open step, a day whose projects have all spent
their budget, a day the machine's whole ceiling has been reached, and a day
whose ledger could not be read are four states an operator would act on
differently, and one word for all four would be a row that only ever says
nothing happened.

**One wake is one turn**, never a loop. A row that worked steps until they ran
out would be a row whose cost is decided by how many steps the morning happened
to propose, and it would hold the app's one unattended conversation open for as
long as that took. Seven wakes at an hour apart is a day with a shape the
operator can watch and interrupt.
"""

from __future__ import annotations

import asyncio
import logging
import time

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

#: What the model calls to finish a step. Counted on the turn, so the row's
#: outcome is what happened rather than what the reply said about it.
_CLOSE = "task_close"

#: The mark this row's turns carry. It has to exist in three places at once:
#: `chat.RUNTIME_ORIGINS`, which REFUSES an origin it does not know, and the
#: transcript's own label map, which would otherwise draw the work under the
#: sentence meant for a turn nobody asked for.
_ORIGIN = "workday"


def _worked_line(closed: int, open_steps: int) -> str:
    """What a wake that ran shows the operator, as a sentence.

    Both numbers, because "it closed one" and "one of six" are different
    afternoons and the second is the one that says whether the day is keeping
    up. One step is not "1 step(s)".
    """
    steps = f"{open_steps} step" + ("" if open_steps == 1 else "s")
    if closed:
        return f"it closed {closed} of {steps} open"
    return f"it worked a step. {steps} still open"


class WorkdayJob(BaseJob):
    """Take the day forward by one step, or say why it could not.

    `uses_llm` is False for `MorningJob`'s reason: the flag draws the per-row
    model dropdown, and the model here is the conversation's, through the one
    funnel, so a per-row override would answer a question this row does not
    ask.
    """

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            return await self._run(ctx, t0)
        except Exception as exc:  # noqa: BLE001 - contract: never raise
            log.exception("workday: the row failed")
            return self._result(
                ctx, t0, RunOutcome.FAILED, f"the day could not be worked: {exc}"
            )

    async def _run(self, ctx: JobContext, t0: float) -> JobResult:
        from tesseract.orchestrator.autonomy.morning import (
            day_is_capped,
            send_one_turn,
            spent_today_by_project,
        )
        from tesseract.orchestrator.autonomy.workday import build, read_open_steps

        # Blocking, all three: one parses a config file and two parse files
        # that only grow. Threaded so health, the websocket heartbeats and
        # every open conversation stay responsive while they are read.
        capped = await asyncio.to_thread(day_is_capped, ctx.app)
        if capped:
            return self._result(ctx, t0, RunOutcome.REFUSED, capped)

        spent = await asyncio.to_thread(spent_today_by_project)
        if spent is None:
            return self._result(
                ctx, t0, RunOutcome.REFUSED,
                "today's spend could not be read, so there is no ceiling to "
                "work against. Nothing was worked and nothing was spent",
            )

        steps = await asyncio.to_thread(read_open_steps, spent)
        prompt = build(steps)
        if not prompt:
            return self._result(
                ctx, t0, RunOutcome.SKIPPED_NO_WORK,
                "nothing is open on a project it may work with room left today",
            )

        said, closed = await send_one_turn(
            ctx.app, prompt, origin=_ORIGIN, counting=_CLOSE
        )
        return self._result(
            ctx, t0, RunOutcome.SUCCEEDED, "",
            closed=closed, open_steps=len(steps), said=said,
        )

    def _result(
        self,
        ctx: JobContext,
        t0: float,
        outcome: RunOutcome,
        reason: str,
        *,
        closed: int = 0,
        open_steps: int = 0,
        said: str = "",
    ) -> JobResult:
        # `ok` is required and is then overwritten from `outcome` in
        # `__post_init__`. It is passed as what the outcome means so the two
        # never read as disagreeing at the call site either.
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=outcome is not RunOutcome.FAILED,
            detail=reason or _worked_line(closed, open_steps),
            outcome=outcome,
            outcome_reason=reason,
            payload={"closed": closed, "open_steps": open_steps, "said": said},
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )
