"""IndexRebuildJob — nightly FAISS + FTS rebuild.

FAISS rebuild requires embeddings (Ollama). When offline, the job still
runs — it records `embedding_available=False` and rebuilds the SQLite
FTS (BM25) index only, which carries no external dependency. This is
the expected degraded mode, so `ok=True` regardless.

Canonical truth lives in `memory-store/*.md`. Both indexes are derived
artifacts — deleting them and running this job recovers a clean state.
"""

from __future__ import annotations

import asyncio
import logging
import time

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class IndexRebuildJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            bundle = _resolve_bundle(ctx)
            if bundle is None or getattr(bundle, "store", None) is None:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="memory_bundle unavailable",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            triples = _collect_triples(bundle.store)
            embedding_available = getattr(bundle, "embeddings", None) is not None

            faiss_count = 0
            if embedding_available:
                pairs = [(mid, body) for mid, _title, body in triples]
                faiss_count = await bundle.embeddings.rebuild_from_store(pairs)

            # FTS rebuild is a synchronous SQLite DELETE+INSERT over every
            # memory (~hundreds of rows) — run it off the event loop so it
            # can't stall Mirror WS / Telegram / heartbeats. Safe off-thread
            # now that FTSIndex uses per-thread connections.
            fts_index = getattr(bundle, "fts_index", None)
            fts_count = (
                await asyncio.to_thread(fts_index.rebuild, triples)
                if fts_index is not None
                else 0
            )

            faiss_detail = str(faiss_count) if embedding_available else "skipped"
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"faiss={faiss_detail} fts={fts_count}",
                payload={
                    "embedding_available": embedding_available,
                    "faiss_count": faiss_count,
                    "fts_count": fts_count,
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("index_rebuild crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_bundle(ctx: JobContext):
    app = ctx.app
    if app is None:
        return None
    if hasattr(app, "get"):
        return app.get("memory_bundle")
    return None


def _collect_triples(store) -> list[tuple[str, str, str]]:
    """Return `[(memory_id, title, body)]` for every live memory."""
    triples: list[tuple[str, str, str]] = []
    for fm in store.list_all():
        entry = store.read(fm.id, log_access=False)
        if entry is None:
            continue
        _, body = entry
        triples.append((fm.id, fm.title, body))
    return triples
