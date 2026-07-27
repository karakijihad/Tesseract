"""ObserverIdleJob — fire `Observer.observe()` when a Mirror session has been idle.

Cadence (`tesseract/config/schedule.yaml` — `observer_idle_trigger`) ticks every
10 minutes; each tick inspects `app["server_sessions"]` and fires the observer
exactly once against the *most-idle* session whose `last_turn_at` is older than
`config.idle_threshold_minutes`. If no sessions, no observer, or nothing idle,
the job is a no-op (`ok=True`) and records the reason in `detail`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class ObserverIdleJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        if "idle_threshold_minutes" not in ctx.config:
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail="missing config key: idle_threshold_minutes",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        threshold_minutes = ctx.config["idle_threshold_minutes"]

        try:
            observer = _get(ctx.app, "observer")
            if observer is None:
                return _done(ctx, t0, ok=True, detail="no_observer")

            sessions = _get(ctx.app, "server_sessions") or {}
            if not sessions:
                return _done(ctx, t0, ok=True, detail="no_active_session")

            threshold_seconds = threshold_minutes * 60
            now = datetime.now(timezone.utc)
            idle_candidates: list[tuple[float, Any]] = []
            for sess in sessions.values():
                last = getattr(sess, "last_turn_at", None)
                if last is None:
                    continue
                idle_s = (now - last).total_seconds()
                if idle_s >= threshold_seconds:
                    idle_candidates.append((idle_s, sess))

            if not idle_candidates:
                return _done(ctx, t0, ok=True, detail="not_idle")

            idle_s, sess = max(idle_candidates, key=lambda pair: pair[0])
            chat_session = getattr(sess, "chat_session", None)
            history = list(getattr(chat_session, "history", []))
            text = await observer.observe(
                history,
                mode="meta",
                session_id=getattr(sess, "session_id", ""),
            )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"fired idle_s={idle_s:.0f}",
                payload={
                    "observation_len": len(text or ""),
                    "session_id": getattr(sess, "session_id", ""),
                    "threshold_minutes": threshold_minutes,
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("observer_idle_trigger crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _get(app, key: str):
    if app is None:
        return None
    if hasattr(app, "get"):
        return app.get(key)
    return None


def _done(ctx: JobContext, t0: float, *, ok: bool, detail: str) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=ok,
        detail=detail,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )
