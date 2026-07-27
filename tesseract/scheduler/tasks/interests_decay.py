"""InterestsDecayJob — daily decay tick for the daily-brief affinity profile.

MO-9-14 — the operator's per-pillar interest weights drift up with
INTERESTED / DIG_DEEPER / COMMENTED signals and down with NOT_FOR_ME.
Without decay, a single one-off reaction would weight a topic forever.
This job applies an exponential half-life (30 days by default) once per
day so forgotten signals erode back to noise; signals reinforced by
fresh reactions keep their pull.

Disabled by default in ``schedule.yaml``. Operator flips on after one
week of brief use so the decay tick has a non-empty profile to work
against.

No-op semantics: when ``<TESSERACT_HOME>/memory-store/interests/
profile.yaml`` does not exist (operator has not engaged with any cards
yet) the job returns ok=True with a descriptive detail rather than
creating an empty file.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from tesseract.orchestrator.brief.interests import (
    DEFAULT_HALF_LIFE_DAYS,
    decay,
    load_profile,
    save_profile,
)
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class InterestsDecayJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            profile_path = _resolve_profile_path(ctx)
            half_life_days = int(
                ctx.config.get("half_life_days", DEFAULT_HALF_LIFE_DAYS)
            )
            days = int(ctx.config.get("days", 1))
            if not profile_path.exists():
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail="profile missing — skipped",
                    payload={
                        "profile_path": str(profile_path),
                        "skipped": True,
                    },
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )
            before = load_profile(profile_path)
            after = decay(before, days=days, half_life_days=half_life_days)
            save_profile(after, profile_path)
            kept = sum(len(topics) for topics in after.pillars.values())
            pruned = (
                sum(len(t) for t in before.pillars.values()) - kept
            )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=(
                    f"decayed half_life_days={half_life_days} days={days} "
                    f"kept={kept} pruned={pruned}"
                ),
                payload={
                    "profile_path": str(profile_path),
                    "half_life_days": half_life_days,
                    "days": days,
                    "kept_topics": kept,
                    "pruned_topics": pruned,
                    "last_decay_at": after.last_decay_at,
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("interests_decay crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_profile_path(ctx: JobContext) -> Path:
    override = ctx.config.get("profile_path")
    if override:
        return Path(override)
    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    return home / "memory-store" / "interests" / "profile.yaml"


__all__ = ["InterestsDecayJob"]
