"""AtlasVerifyJob — prove the map still matches the library it claims to map.

Weekly, and cheap: it re-derives the graph in memory and compares structure.
A rebuild guarantee that is never exercised is how a derived tree quietly
becomes primary, so this is the exercise.

Drift is `degraded`, never `failed`. The atlas being out of step is a fact
about the atlas, not a broken job — the run did exactly what it was asked and
found something. Reporting it as a failure would put it in the same bucket as
a crash and teach the operator to read neither.
"""

from __future__ import annotations

import asyncio
import logging
import time

from tesseract.orchestrator.atlas.verify import run_verify
from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.tasks.atlas_build import (
    _resolve_memory_store,
    _resolve_vault_manager,
)
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class AtlasVerifyJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            report = await asyncio.to_thread(
                run_verify,
                memory_store=_resolve_memory_store(ctx),
                vault_manager=_resolve_vault_manager(ctx),
                now=ctx.fired_at,
            )
            payload = {
                "drift": report.drift.count,
                "missing": list(report.drift.missing),
                "extra": list(report.drift.extra),
                "changed": list(report.drift.changed),
                "live_nodes": report.live_nodes,
                "rebuilt_nodes": report.rebuilt_nodes,
                "stale_version": report.stale_version,
            }
            duration = (time.monotonic() - t0) * 1000.0
            if report.stale_version:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail="atlas predates the running builder",
                    payload=payload,
                    duration_ms=duration,
                    outcome=RunOutcome.DEGRADED,
                    outcome_reason=(
                        "the atlas on disk was built by an older builder, so it "
                        "was compared against one it cannot match; the next "
                        "build re-derives it"
                    ),
                )
            if not report.drift.clean:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail=report.drift.describe(),
                    payload=payload,
                    duration_ms=duration,
                    outcome=RunOutcome.DEGRADED,
                    outcome_reason=(
                        f"the atlas no longer matches its inputs: "
                        f"{report.drift.describe()}"
                    ),
                )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"no drift over {report.live_nodes} node(s)",
                payload=payload,
                duration_ms=duration,
                outcome=RunOutcome.SUCCEEDED,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("atlas_verify crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
                outcome=RunOutcome.FAILED,
                outcome_reason=f"the check crashed: {type(exc).__name__}",
            )


__all__ = ["AtlasVerifyJob"]
