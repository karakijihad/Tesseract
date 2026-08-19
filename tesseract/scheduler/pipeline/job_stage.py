"""An existing scheduler job, wrapped as a stage.

The stage bodies ARE the job bodies: nothing about what a job does changes when
it moves onto the pipeline. What the wrapper adds is the part cron could not —
the declared edges, and a result that says what came of the run rather than
only whether it raised.

Two jobs make that last part necessary rather than decorative. `memory_lint`
and `vault_lint` both return `ok=False` when they FIND something, because
`on_failure: alert` was the only way to reach the operator. Read literally by a
pipeline, a lint that found work would fail and `memory_scrub` would be skipped
for upstream failure — exactly backwards. So a stage may map its job's result
itself, and those two do.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Callable

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.pipeline.stage import (
    Stage,
    StageCadence,
    StageContext,
    StageKind,
    StageReport,
)
from tesseract.scheduler.types import JobContext, JobResult

# Given the job's result, what came of the run. Returning `None` falls through
# to the default reading.
ResultReader = Callable[[JobResult], StageReport | None]

# How much of a stage's budget a multi-day catch-up will spend before stopping
# itself. The remainder is the margin that keeps the runner's own timeout from
# cancelling the walk with nothing reported.
_BUDGET_HEADROOM = 0.8


def default_report(result: JobResult) -> StageReport:
    """The reading for a job that has not declared its own.

    `JobResult` already carries an outcome (AR-1) — derived from `ok` for the
    call sites that have not declared one — so this is a translation, not a
    second opinion.
    """
    outcome = result.outcome or RunOutcome.SUCCEEDED
    reason = result.outcome_reason or result.detail
    if outcome is not RunOutcome.SUCCEEDED and not reason.strip():
        reason = "the job did not say why"
    return StageReport(outcome=outcome, reason=reason)


def job_stage(
    *,
    name: str,
    job: type[BaseJob],
    reads: tuple[str, ...] = (),
    writes: tuple[str, ...] = (),
    after: tuple[str, ...] = (),
    cadence: StageCadence = StageCadence.DAILY,
    kind: StageKind = StageKind.DETERMINISTIC,
    budget_seconds: float = 300.0,
    retries: int = 0,
    retry_backoff_seconds: float = 0.0,
    report: ResultReader = default_report,
    per_day: bool = False,
    max_catchup_days: int = 7,
) -> Stage:
    """Declare `job` as a stage. The module keeps its own home under
    `scheduler/tasks/` and stays importable and runnable on its own.

    `per_day=True` marks a job that **cannot cover a gap by being called
    once**, so the wrapper walks the missed days instead of handing it a window
    it does not read. Two shapes qualify: one that derives a single calendar
    day from `ctx.fired_at` — `chat_digest`, `daily_writer` and `feedback_sweep`
    all do (`(fired_at - 1 day).date()`) — and one that selects against a bounded
    window ending there, which `conversation_reflect` does with its lookback.
    Overlapping windows are safe on the second kind because the job keeps a
    position per item; without one, a walk would redo work rather than resume.
    """

    async def body(ctx: StageContext) -> StageReport:
        # Stamped here, not inside the walk: the runner's own timeout starts
        # when this body is entered, so a deadline measured from any later
        # point drifts past it — and the runner cancels a body with nothing
        # reported, losing every day already covered.
        deadline = time.monotonic() + budget_seconds * _BUDGET_HEADROOM
        # The versions this stage is about to consume, recorded BEFORE it runs
        # — `reads` on the manifest is the contract's account of what a stage
        # read, and reading it afterwards would name whatever the run had
        # become by then. A field nothing populates is a field nobody can
        # trust, which is what it was.
        for artifact in ctx.stage.reads:
            ctx.read(artifact)
        if per_day:
            stage_report = await _run_per_day(
                ctx, job, name, report, max_catchup_days, deadline
            )
        else:
            result = await job().run(_job_context(ctx, name))
            stage_report = report(result) or default_report(result)
        if stage_report.outcome is RunOutcome.SUCCEEDED and stage_report.changed:
            # A wrapped job publishes its declared outputs when it produced
            # something. `skipped_no_work` deliberately does not, and neither
            # does a run that succeeded having changed nothing — a multi-day
            # catch-up over quiet days succeeds with changed=0, and an artifact
            # version incrementing on that would tell every reader downstream
            # there is something new to look at when there is not.
            for artifact in ctx.stage.writes:
                ctx.write(artifact)
        return stage_report

    return Stage(
        name=name,
        body=body,
        reads=reads,
        writes=writes,
        after=after,
        cadence=cadence,
        kind=kind,
        budget_seconds=budget_seconds,
        retries=retries,
        retry_backoff_seconds=retry_backoff_seconds,
        per_day=per_day,
    )


async def _run_per_day(
    ctx: StageContext,
    job: type[BaseJob],
    name: str,
    report: ResultReader,
    max_catchup_days: int,
    deadline: float,
) -> StageReport:
    """Run a date-scoped job once for every day its watermark says it missed.

    The runner advances a successful stage's watermark to the whole window's
    end, so a job that only ever looks at "yesterday relative to fired_at"
    would process one day after a week of downtime and have the other six
    marked done — silently and permanently, since each of these jobs is
    idempotent per date and never looks back. The window is real; this is what
    makes the job see it.

    Bounded by `max_catchup_days` because two of the three call a model: a
    month-long gap must not become a month of provider calls in one night.
    Beyond the bound the stage is `truncated` and reports how far it actually
    got, so the next run resumes from there rather than skipping the rest.
    """
    days = _missed_days(ctx, max_catchup_days)
    if len(days) <= 1:
        result = await job().run(_job_context(ctx, name))
        return report(result) or default_report(result)

    # The walk shares the stage's one wallclock budget, and `deadline` is
    # stamped by the caller at the instant the runner started its timeout.
    # Stopping short of it deliberately: a cancelled body reports nothing, so
    # every day already done would be redone next run — which for the two
    # model stages means paying for them twice.
    reports: list[StageReport] = []
    covered: datetime | None = None
    ran_out = ""
    failure = ""
    for index, day in enumerate(days):
        if index and time.monotonic() >= deadline:
            ran_out = "ran out of its budget"
            break
        result = await job().run(_job_context(ctx, name, fired_at=day))
        day_report = report(result) or default_report(result)
        if day_report.outcome is RunOutcome.FAILED:
            failure = f"{day.date().isoformat()} failed: {day_report.reason}"
            break
        reports.append(day_report)
        covered = day

    changed = sum(r.changed for r in reports)
    refused = sum(r.refused for r in reports)
    if covered is None:
        # The first day it tried failed, so nothing was covered and there is no
        # position worth recording.
        return StageReport(
            outcome=RunOutcome.FAILED,
            reason=f"catch-up stopped at the first missed day — {failure or ran_out}",
        )
    if failure:
        # A day that errored is a failure, and it says so — `degraded` keeps
        # the run OK, which would have turned a provider dying mid-catch-up
        # into a clean-looking night and silenced the row's `on_failure:
        # alert`. The watermark still records the days that DID land, so
        # saying "failed" costs nothing already earned.
        return StageReport(
            outcome=RunOutcome.FAILED,
            reason=(
                f"caught up {len(reports)} of {len(days)} missed days to "
                f"{covered.date().isoformat()}, then {failure}"
            ),
            changed=changed,
            refused=refused,
            watermark=covered,
        )
    if ran_out or covered < ctx.window_end:
        return StageReport(
            outcome=RunOutcome.TRUNCATED,
            reason=(
                f"caught up {len(reports)} of {len(days)} missed days to "
                f"{covered.date().isoformat()}"
                + (f" — {ran_out}" if ran_out else "")
                + "; the rest resumes next run"
            ),
            changed=changed,
            refused=refused,
            watermark=covered,
        )
    return StageReport(
        outcome=RunOutcome.SUCCEEDED,
        reason=f"caught up {len(reports)} missed days",
        changed=changed,
        refused=refused,
        # Only as far as it actually got. Days already done are not repeated,
        # and days not reached are not skipped.
        watermark=covered,
    )


def _missed_days(ctx: StageContext, max_catchup_days: int) -> list[datetime]:
    """Every anchor-equivalent instant this stage still owes, OLDEST FIRST.

    Oldest first is the whole point: a bounded catch-up has to start at the
    watermark and work forward, so the days it does not reach are the recent
    ones the next run will cover. Taking the newest N instead would leave a
    permanent hole at the old end — the very defect this exists to close.

    One entry (the anchor) when there is no gap: a first run, or the ordinary
    nightly case, so the common path is unchanged.
    """
    if ctx.window_start is None:
        return [ctx.window_end]
    gap_days = (ctx.window_end.date() - ctx.window_start.date()).days
    if gap_days <= 1:
        return [ctx.window_end]
    covered = min(gap_days, max_catchup_days)
    first = ctx.window_start + timedelta(days=1)
    return [first + timedelta(days=offset) for offset in range(covered)]


def _job_context(
    ctx: StageContext, name: str, *, fired_at: datetime | None = None
) -> JobContext:
    app: Any = ctx.app
    role = ctx.config.get("model_role")
    chain = ctx.config.get("chain")
    return JobContext(
        job_name=name,
        run_id=ctx.run_id,
        fired_at=fired_at or ctx.anchor,
        app=app,
        config=dict(ctx.config),
        log_dir=ctx.log_dir,
        # The per-job role override survives the collapse: it was a row-level
        # key and is now a key on the stage's own config block. `None` means
        # the handler's `default_model_role`, exactly as before.
        model_role=str(role) if role else None,
        # A stage may name a chain instead, for work that was never a pillar.
        # What it spends bills to the row, not to the stage: the row is the
        # manifest entry, and the entry is where a ceiling can be declared.
        model_chain=str(chain) if chain else None,
        billing_key=ctx.entry or name,
        cost_ledger=(app.get("cost_ledger") if hasattr(app, "get") else None),
        trigger_source="scheduled",
    )


def _tally(payload: dict[str, Any], keys: tuple[str, ...]) -> int:
    total = 0
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            total += int(value)
        elif isinstance(value, (int, float)):
            total += int(value)
        elif isinstance(value, (list, tuple, set, dict)):
            total += len(value)
    return total


def payload_counts(
    *,
    changed: tuple[str, ...] = (),
    refused: tuple[str, ...] = (),
    quiet: str = "there was nothing to do",
) -> ResultReader:
    """Read a job's own payload for the two numbers every stage owes.

    The counts were always in there — `promoted=1`, `sections_written=0`,
    `auto_ingested` — and every one of them was thrown away at the boundary,
    which is why a night where nothing happened and a night where everything
    did looked identical on the health surface.

    A run that changed nothing AND refused nothing is `skipped_no_work`: it
    ran, there was no work, and that is healthy. Saying `succeeded` would be
    the same lie one level down that AR-1 removed one level up.
    """

    def read(result: JobResult) -> StageReport:
        payload = result.payload if isinstance(result.payload, dict) else {}
        made = _tally(payload, changed)
        declined = _tally(payload, refused)
        if not result.ok:
            return counted(
                RunOutcome.FAILED,
                result.detail or "the job failed without saying why",
                changed=made,
                refused=declined,
            )
        if not made and not declined:
            return counted(
                RunOutcome.SKIPPED_NO_WORK,
                f"{quiet} ({result.detail})" if result.detail else quiet,
            )
        return counted(
            RunOutcome.SUCCEEDED, result.detail, changed=made, refused=declined
        )

    return read


def counted(
    outcome: RunOutcome,
    reason: str = "",
    *,
    changed: int = 0,
    refused: int = 0,
) -> StageReport:
    """A report with its two counts. Every stage owes both — what it changed
    and what it declined to — and not one of the eighteen jobs reported the
    second before this."""
    return StageReport(outcome=outcome, reason=reason, changed=changed, refused=refused)


__all__ = [
    "ResultReader",
    "counted",
    "default_report",
    "job_stage",
    "payload_counts",
]
