from __future__ import annotations

import logging
import time
from typing import Any

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class AlarmHandlerJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        cfg = ctx.config
        alarm_name = cfg.get("alarm_name") or cfg.get("alarm_label", "")
        alarm_id = cfg.get("alarm_id", "")
        message = cfg.get("message", "")
        recurring = bool(cfg.get("recurring", False))
        snooze_options = list(cfg.get("snooze_options") or [])
        base_payload = {
            "alarm_id": alarm_id,
            "alarm_name": alarm_name,  # back-compat — S4 frontend reads this
            "alarm_label": alarm_name,
            "message": message,
            "recurring": recurring,
            "snooze_options": snooze_options,
        }
        try:
            sessions = _server_sessions(ctx.app)
            if not sessions:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail="no_active_ws",
                    payload={"alarm_name": alarm_name, "message": message},
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )
            delivered = await _broadcast(sessions, base_payload)
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"delivered to {delivered} ws",
                payload={
                    "alarm_name": alarm_name,
                    "message": message,
                    "delivered_ws_count": delivered,
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("alarm_handler crashed for %s", alarm_name)
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _server_sessions(app: Any) -> dict[str, Any]:
    if app is None or not hasattr(app, "get"):
        return {}
    return app.get("server_sessions") or {}


async def _broadcast(sessions: dict[str, Any], payload: dict[str, Any]) -> int:
    """Fan a `schedule_alarm_fired` envelope out to every live WS."""
    from tesseract.mirror.server.envelope import make_envelope
    from tesseract.mirror.server.session import send_envelope

    delivered = 0
    for sess in sessions.values():
        env = make_envelope(
            "schedule_alarm_fired",
            "schedule",
            getattr(sess, "session_id", ""),
            payload,
        )
        try:
            await send_envelope(sess, env)
            delivered += 1
        except Exception:
            log.exception("alarm_handler: send_envelope failed for %s", getattr(sess, "session_id", "?"))
    return delivered
