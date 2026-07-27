"""WorkIndexSweepJob — daily maintenance for the derived indexes.

Two sweeps in one tick:

1. ``SessionMetadataIndex.prune_orphans()`` — drops session-metadata
   rows whose ``file_path`` no longer exists on disk. Catches deletes
   that bypassed the ``delete_session`` write-through (operator
   ``rm`` from a shell, external sync, etc.).
2. ``WorkIndex.prune_orphans()`` — drops session + workshop chunks
   whose ``source_path`` is gone. Catches the same class of drift
   plus workshop files moved/renamed outside the tools.

Both indexes are derived from canonical files; a full rebuild is
always available via ``python -m tesseract.scripts.work_index_backfill``.
This job is the cheap nightly pass that keeps the indexes from
accumulating ghost rows between full rebuilds.

The job is read-then-prune — no LLM call, no network. Sub-second
on a typical corpus. Per CLAUDE.md "BaseJob contract forbids
raising"; every error path returns ``JobResult(ok=False, …)``.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


def _home() -> Path:
    """Canonical env-or-default home resolution. Matches the hook
    helpers in ``session_store.py`` and ``file_write.py``."""
    from tesseract.paths import TESSERACT_HOME as _DEFAULT_HOME

    return Path(os.environ.get("TESSERACT_HOME") or _DEFAULT_HOME)


class WorkIndexSweepJob(BaseJob):
    """Prune orphan rows from the two CR-1 derived indexes."""

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        sm_pruned = 0
        wi_pruned = 0
        errors: list[str] = []

        try:
            from tesseract.memory.session_metadata import SessionMetadataIndex
            home = _home()
            sm = SessionMetadataIndex(home / "session_metadata.sqlite")
            try:
                sm_pruned = sm.prune_orphans()
            finally:
                sm.close()
        except Exception as exc:  # noqa: BLE001
            log.exception("work_index_sweep: session_metadata prune failed")
            errors.append(f"session_metadata: {exc!r}")

        try:
            from tesseract.memory.work_index import WorkIndex
            home = _home()
            wi = WorkIndex(home / "work_index.sqlite")
            try:
                wi_pruned = wi.prune_orphans()
            finally:
                wi.close()
        except Exception as exc:  # noqa: BLE001
            log.exception("work_index_sweep: work_index prune failed")
            errors.append(f"work_index: {exc!r}")

        ok = not errors
        detail_parts = [
            f"session_metadata_pruned={sm_pruned}",
            f"work_index_paths_pruned={wi_pruned}",
        ]
        if errors:
            detail_parts.append("errors=" + "; ".join(errors))
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=ok,
            detail=" ".join(detail_parts),
            payload={
                "session_metadata_pruned": sm_pruned,
                "work_index_paths_pruned": wi_pruned,
                "errors": errors,
            },
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )
