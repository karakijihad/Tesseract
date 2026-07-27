"""MemoryLintJob — scheduled memory-store integrity scan.

Read-only counterpart to `vault_lint`. Uses `tesseract/memory/memory_lint.py`
to detect broken wikilinks, broken frontmatter `auto_links`/`links`, stale
`source_path` values, and zero-byte Obsidian stub files. Repairs are
operator-driven — the job reports loud (`ok=False`) when findings exist
so a toast surfaces in the Mirror.

Does not overlap with:
  - `dream_cycle.sweep_missing_wikilinks` — that ADDS missing wikilinks
    when an entry has `source_path` but no body wikilink. This job
    detects when EXISTING links don't resolve.
  - `librarian_heartbeat` — promotes daily/ entries; doesn't validate
    frontmatter cross-references.
  - `index_rebuild` — derived FAISS/FTS rebuild; doesn't read bodies
    for link integrity.
"""

from __future__ import annotations

import logging
import time

from tesseract.memory.memory_lint import MemoryLinter
from tesseract.paths import ROOT, TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class MemoryLintJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            bundle = _resolve_bundle(ctx)
            if bundle is None:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="memory_bundle unavailable",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            store = getattr(bundle, "store", None)
            store_dir = getattr(store, "store_dir", None) if store is not None else None
            if store_dir is None:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="memory_bundle.store.store_dir unavailable",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            linter = MemoryLinter(
                store_dir=store_dir,
                project_root=TESSERACT_HOME,
                repo_root=ROOT,
            )
            report = linter.lint()

            detail = (
                f"wikilinks={len(report.broken_wikilinks)} "
                f"fm_links={len(report.broken_frontmatter_links)} "
                f"stale_sources={len(report.stale_source_paths)} "
                f"orphans={len(report.orphan_stubs)}"
            )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=report.total == 0,
                detail=detail,
                payload=report.as_dict(),
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:
            log.exception("memory_lint job crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_bundle(ctx: JobContext):
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return None
    return app.get("memory_bundle")
