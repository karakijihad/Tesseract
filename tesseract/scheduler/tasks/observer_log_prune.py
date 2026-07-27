"""ObserverLogPruneJob — delete observer JSONL logs older than N days.

`tesseract/brain/observer.py::_append_observation_log` writes one record
per observation into `tesseract/logs/observer/YYYY-MM-DD.jsonl`. The
files were never read back, never pruned — left running long enough they
accumulate without bound. Operator obs #1 (2026-04-30 brainstorm).

Cron-driven; cadence + retention live in `tesseract/config/schedule.yaml`
under `observer_log_prune.config.retention_days`. Default behavior when
the directory is missing is a clean no-op (returns ok=True, deleted=0)
so a fresh checkout doesn't show a fake failure on first heartbeat.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class ObserverLogPruneJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        retention_days = int(ctx.config.get("retention_days", 14))
        log_dir = _resolve_log_dir(ctx)
        try:
            deleted = _prune_old_logs(log_dir, retention_days)
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"deleted={deleted} retention_days={retention_days}",
                payload={"deleted": deleted, "retention_days": retention_days},
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("observer_log_prune crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_log_dir(ctx: JobContext) -> Path:
    """Resolve the observer log dir from app context, with a sane fallback.

    The job runs inside the Mirror backend, so `app['tesseract_dir']` is
    populated. Tests inject `app['observer_log_dir']` directly to avoid
    touching the real logs/ tree.
    """
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        explicit = app.get("observer_log_dir")
        if explicit is not None:
            return Path(explicit)
        tess = app.get("tesseract_dir")
        if tess is not None:
            return Path(tess) / "logs" / "observer"
    # Module fallback — matches `_OBSERVER_LOG_DIR` in observer.py.
    from tesseract.brain.observer import _OBSERVER_LOG_DIR

    return _OBSERVER_LOG_DIR


def _prune_old_logs(log_dir: Path, retention_days: int) -> int:
    """Delete JSONL files whose stem (YYYY-MM-DD) is older than the cutoff.

    Returns the number of files deleted. A missing directory is a no-op.
    Files with non-date stems are ignored — promoted-and-renamed files
    (none exist today, but reserve the convention) survive.
    """
    if not log_dir.exists():
        return 0
    cutoff = date.today() - timedelta(days=retention_days)
    deleted = 0
    for path in log_dir.glob("*.jsonl"):
        try:
            stem_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if stem_date < cutoff:
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                log.warning("observer log prune failed for %s: %s", path, exc)
    return deleted
