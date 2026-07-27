"""AU-16 S2 — ``DigestDailyJob``.

Daily UTC rollup of every seal produced on a given date into one global
digest file at ``memory-store/trees/global/<YYYY-MM-DD>.md``. The job
is fully idempotent — it recomputes the day's file from the seal store
every run, so reruns are cheap and converge to the same state.

By default the job digests *today* (UTC). Callers can pass
``ctx.config["target_date"]`` (ISO date string) to rebuild a specific
day, useful when backfilling after a long downtime.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

from tesseract.memory.leaf_seals import Seal, iter_seals
from tesseract.memory.trees.global_tree import write_daily_digest
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class DigestDailyJob(BaseJob):
    """Per-day global digest rebuild.

    Configuration via ``ctx.config``:

    - ``target_date``: ISO date string. Defaults to ``ctx.fired_at`` UTC.
    """

    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        raw = ctx.config.get("target_date")
        try:
            target = (
                date.fromisoformat(raw)
                if raw
                else ctx.fired_at.astimezone(timezone.utc).date()
            )
        except ValueError:
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"invalid target_date {raw!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

        seals_today: list[Seal] = []
        for seal in iter_seals():
            if seal.sealed_at.astimezone(timezone.utc).date() == target:
                seals_today.append(seal)

        path = write_daily_digest(target, seals_today)

        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail=(
                f"target={target.isoformat()} seals={len(seals_today)} path={path.name}"
            ),
            payload={
                "target_date": target.isoformat(),
                "seals": len(seals_today),
                "path": str(path),
            },
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )


__all__ = ["DigestDailyJob"]
