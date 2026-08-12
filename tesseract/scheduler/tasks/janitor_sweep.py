"""JanitorSweepJob — scheduled fingerprint-and-orphan cleanup.

Wraps `tesseract.janitor.run_sweep`. Kill
policy is fixed in janitor code (fingerprint AND orphan); cadence lives
in schedule.yaml only.
"""

from __future__ import annotations

import asyncio
import logging
import time

from tesseract.janitor import run_sweep
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class JanitorSweepJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            # psutil scans + rmtree are blocking — keep the event loop hot.
            report = await asyncio.to_thread(run_sweep, dry_run=False)
        except Exception as exc:  # noqa: BLE001 — contract: never raise
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"sweep raised: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=not report.errors,
            detail=report.summary(),
            payload={
                "findings": len(report.findings),
                "errors": list(report.errors),
            },
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )
