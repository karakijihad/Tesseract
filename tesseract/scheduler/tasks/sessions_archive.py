"""SessionsArchiveJob — move per-run session files older than N days
into `<TESSERACT_HOME>/sessions/archive/YYYY-MM/`.

Phase 1 of the CLI-parity plan (2026-05-10). Operator chose 7 days
as the active retention window — the live SessionDrawer surfaces one
calendar week of context, the archive (lazy-fetched) holds everything
older.

Files with non-canonical names (custom-saved by the operator like
`before-rebase`) stay where they are — only `YYYY-MM-DD-HHMM.json`
files are eligible.

The actual move logic lives in `tesseract/brain/session_store.py::
archive_old_sessions` so the REPL and Mirror can share the helper.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from tesseract.brain.session_store import ARCHIVE_AGE_DAYS, archive_old_sessions
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class SessionsArchiveJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        retention_days = int(ctx.config.get("retention_days", ARCHIVE_AGE_DAYS))
        sessions_dir = _resolve_sessions_dir(ctx)
        try:
            # Off the loop: globs the sessions dir and moves every file
            # past the retention window.
            moved = await asyncio.to_thread(
                archive_old_sessions, sessions_dir, days=retention_days
            )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"archived={len(moved)} retention_days={retention_days}",
                payload={
                    "archived": len(moved),
                    "retention_days": retention_days,
                    "destinations": [str(p) for p in moved],
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("sessions_archive crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_sessions_dir(ctx: JobContext) -> Path:
    """Resolve the sessions dir from app context, with a sane fallback.

    Mirror backend populates `app['tesseract_dir']`. Tests inject
    `app['sessions_dir']` to scope writes away from the real tree.
    """
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        explicit = app.get("sessions_dir")
        if explicit is not None:
            return Path(explicit)
        tess = app.get("tesseract_dir")
        if tess is not None:
            return Path(tess) / "sessions"
    # Module fallback (no `app` — REPL/tests without Mirror context).
    # Call-time home_dir(), not a `Path(__file__)` code-tree anchor —
    # sibling fix alongside the alarms state relocation;
    # inert in production since Mirror always sets `app['tesseract_dir']`.
    from tesseract.paths import home_dir

    return home_dir() / "sessions"
