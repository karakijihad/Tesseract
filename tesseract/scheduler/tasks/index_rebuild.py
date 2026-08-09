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

from tesseract.memory.consistency import DAILY_FTS_PREFIX
from tesseract.memory.vault_indexer import VAULT_ID_PREFIX
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

# The FTS table and FAISS index are shared with content this job cannot
# re-feed from the memory store: vault chunks (FTS + FAISS) and daily-note
# rows (FTS only). A rebuild must leave those in place.
_FTS_PRESERVE = (VAULT_ID_PREFIX, DAILY_FTS_PREFIX)
_FAISS_PRESERVE = (VAULT_ID_PREFIX,)


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
                faiss_count = await bundle.embeddings.rebuild_from_store(
                    pairs, preserve_prefixes=_FAISS_PRESERVE
                )

            # FTS rebuild is a synchronous SQLite DELETE+INSERT over every
            # memory (~hundreds of rows) — run it off the event loop so it
            # can't stall Mirror WS / Telegram / heartbeats. Safe off-thread
            # now that FTSIndex uses per-thread connections.
            fts_index = getattr(bundle, "fts_index", None)
            fts_count = (
                await asyncio.to_thread(fts_index.rebuild, triples, _FTS_PRESERVE)
                if fts_index is not None
                else 0
            )

            # Replay pass — any mutation that landed between the snapshot
            # above and the index swaps was clobbered by the rebuild:
            # a NEW memory matches no preserve prefix and vanished, an
            # UPDATED one reverted to snapshot text, and a DELETED one was
            # resurrected from the snapshot. Re-collect and reconcile all
            # three against the canonical files.
            snapshot_by_id = {mid: (title, body) for mid, title, body in triples}
            replayed = 0
            removed = 0
            post_ids: set[str] = set()
            for mid, title, body in await asyncio.to_thread(
                _collect_triples, bundle.store
            ):
                post_ids.add(mid)
                if snapshot_by_id.get(mid) == (title, body):
                    continue
                if fts_index is not None:
                    await asyncio.to_thread(fts_index.add, mid, title, body)
                if embedding_available:
                    try:
                        await bundle.embeddings.add(mid, body)
                    except Exception:
                        log.warning("index_rebuild: replay embed failed for %s", mid)
                replayed += 1
            for mid in set(snapshot_by_id) - post_ids:
                if fts_index is not None:
                    await asyncio.to_thread(fts_index.delete, mid)
                if embedding_available:
                    bundle.embeddings.remove(mid)
                removed += 1

            # Vault vector catch-up — chunks indexed while embeddings were
            # offline exist in FTS but never got vectors, and nothing else
            # revisits them (the watcher's SHA cursor suppresses re-ingest).
            vault_backfilled = 0
            if embedding_available and fts_index is not None:
                faiss_ids = set(bundle.embeddings.snapshot_ids())
                for vid in await asyncio.to_thread(fts_index.all_ids):
                    if not vid.startswith(VAULT_ID_PREFIX) or vid in faiss_ids:
                        continue
                    body = await asyncio.to_thread(fts_index.get_body, vid)
                    if not body:
                        continue
                    try:
                        if await bundle.embeddings.add(vid, body):
                            vault_backfilled += 1
                    except Exception:
                        log.warning("index_rebuild: vault backfill failed for %s", vid)

            faiss_detail = str(faiss_count) if embedding_available else "skipped"
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=(
                    f"faiss={faiss_detail} fts={fts_count} replayed={replayed} "
                    f"removed={removed} vault_backfilled={vault_backfilled}"
                ),
                payload={
                    "embedding_available": embedding_available,
                    "faiss_count": faiss_count,
                    "fts_count": fts_count,
                    "replayed": replayed,
                    "removed": removed,
                    "vault_backfilled": vault_backfilled,
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
