"""One schedule row, running its subgraph and reporting what came of it.

Both rows go through here, so the two `schedule.yaml` entries differ only in
their cadence and their stages. What the operator sees for a row is the sum of
what its stages said — never a bare OK over a run where half of it refused.
"""

from __future__ import annotations

import logging

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.pipeline.manifest import (
    ManifestStore,
    MemoryManifestStore,
    RunManifest,
)
from tesseract.scheduler.pipeline.registry import Row
from tesseract.scheduler.pipeline.runner import PipelineRunner
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


async def run_row(
    row: Row,
    ctx: JobContext,
    *,
    manifests: ManifestStore | MemoryManifestStore | None = None,
) -> JobResult:
    runner = PipelineRunner(
        row.stages,
        manifests=manifests,
        app=ctx.app,
        config=dict(ctx.config),
        log_dir=ctx.log_dir,
        external_reads=row.external_reads,
        entry=row.name,
    )
    manifest = await runner.run(anchor=ctx.fired_at)
    return row_result(row, manifest, ctx)


def row_result(row: Row, manifest: RunManifest, ctx: JobContext) -> JobResult:
    """The row's own outcome, read from its stages' outcomes.

    Worst-case wins, and the reason names the stage that caused it — a row
    that reports OK while one of its stages failed is the defect this whole
    plan started from, one level up.
    """
    outcomes = [row_.outcome for row_ in manifest.rows]
    detail = " · ".join(
        f"{r.stage}:{r.outcome.value}"
        + (f" {r.changed}/{r.refused}" if r.changed or r.refused else "")
        for r in manifest.rows
    )
    if manifest.not_due:
        detail = f"{detail} · not due: {', '.join(manifest.not_due)}".lstrip(" ·")
    if manifest.disabled:
        detail = f"{detail} · disabled: {', '.join(manifest.disabled)}".lstrip(" ·")

    worst = _worst(outcomes)
    reason = ""
    if worst is not RunOutcome.SUCCEEDED:
        culprit = next((r for r in manifest.rows if r.outcome is worst), None)
        reason = (
            f"{culprit.stage}: {culprit.reason}"
            if culprit is not None and culprit.reason
            else f"the {row.name} row ended {worst.value}"
        )
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=worst not in (RunOutcome.FAILED, RunOutcome.SKIPPED_UPSTREAM_FAILED),
        detail=detail or (
            "every stage is disabled" if manifest.disabled else "no stage was due"
        ),
        payload={
            "run_id": manifest.run_id,
            "stages": [r.to_dict() for r in manifest.rows],
            "not_due": list(manifest.not_due),
            "disabled": list(manifest.disabled),
        },
        outcome=worst,
        outcome_reason=reason,
    )


# Worst first. `skipped_no_work` sits above `succeeded` deliberately: a row
# where one stage found nothing and another did real work has succeeded, and
# only a row where NOTHING was due reports that it had nothing to do.
_SEVERITY: tuple[RunOutcome, ...] = (
    RunOutcome.FAILED,
    RunOutcome.SKIPPED_UPSTREAM_FAILED,
    RunOutcome.DEGRADED,
    RunOutcome.TRUNCATED,
    RunOutcome.REFUSED,
    RunOutcome.SUCCEEDED,
    RunOutcome.SKIPPED_NO_WORK,
)


def _worst(outcomes: list[RunOutcome]) -> RunOutcome:
    if not outcomes:
        return RunOutcome.SKIPPED_NO_WORK
    for candidate in _SEVERITY:
        if candidate in outcomes:
            return candidate
    return RunOutcome.SUCCEEDED


__all__ = ["row_result", "run_row"]
