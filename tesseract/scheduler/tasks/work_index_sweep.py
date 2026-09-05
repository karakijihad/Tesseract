"""WorkIndexSweepJob — daily maintenance for the derived records.

Three sweeps in one tick:

1. ``ChatMetadataIndex.prune_orphans()`` — drops chat-metadata rows
   whose ``file_path`` no longer exists on disk. Catches deletes that
   bypassed the ``chat_store.delete_chat`` write-through (operator
   ``rm`` from a shell, external sync, etc.).
2. ``WorkIndex.prune_orphans()`` — drops session + workshop chunks
   whose ``source_path`` is gone. Catches the same class of drift
   plus workshop files moved/renamed outside the tools.
3. Conversation recaps whose transcript is gone get stamped
   ``source_deleted_at``. ``delete_chat`` does this write-through, in two
   steps: unlink the record, then stamp the memory. A crash between them
   leaves a recap claiming a conversation that no longer exists, and until
   this leg there was nothing that would ever notice. Nothing is pruned
   here: the lesson persists and loses only the way back.

Both indexes are derived from canonical files; a full rebuild is
always available via ``python -m tesseract.scripts.work_index_backfill``.
This job is the cheap nightly pass that keeps the indexes from
accumulating ghost rows between full rebuilds.

The job is read-then-prune — no LLM call, no network. Sub-second
on a typical corpus. The ``BaseJob`` contract forbids raising, so
every error path returns ``JobResult(ok=False, …)``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


def _home() -> Path:
    """Canonical env-or-default home resolution. Matches the helpers in
    ``chat_store.py`` and ``file_write.py``."""
    from tesseract.paths import TESSERACT_HOME as _DEFAULT_HOME

    return Path(os.environ.get("TESSERACT_HOME") or _DEFAULT_HOME)


def _prune_chat_metadata_sync(home: Path) -> int:
    """Open, prune, and close in one thread — sqlite3 connections are
    bound to the thread that created them."""
    from tesseract.memory.chat_metadata import ChatMetadataIndex

    cm = ChatMetadataIndex(home / "chat_metadata.sqlite")
    try:
        return cm.prune_orphans()
    finally:
        cm.close()


def _prune_work_index_sync(home: Path) -> int:
    from tesseract.memory.work_index import WorkIndex

    wi = WorkIndex(home / "work_index.sqlite")
    try:
        return wi.prune_orphans()
    finally:
        wi.close()


def _stamp_orphan_recaps_sync(home: Path) -> int:
    """Stamp every recap whose conversation is gone. Returns how many.

    The recap carries the two tags it was written with, so its key is read
    back off the record rather than reversed out of its id, which is a hash
    and reverses out of nothing.

    Idempotent by way of `mark_source_deleted`: an already-stamped record
    keeps its first stamp, so this counts what it newly found rather than
    re-dating every deletion on every nightly pass.
    """
    from tesseract.capture.reflect import REFLECTION_TAG, mark_source_deleted
    from tesseract.capture.sources import live_conversation_keys
    from tesseract.memory.store import MemoryStore

    root = home / "memory-store"
    if not root.is_dir():
        return 0
    live = live_conversation_keys()
    store = MemoryStore(root)
    stamped = 0
    for frontmatter in store.list_all():
        if REFLECTION_TAG not in (frontmatter.tags or []):
            continue
        if frontmatter.source_deleted_at is not None:
            continue
        key = _recap_key(frontmatter.tags)
        if key is None or key in live:
            continue
        if mark_source_deleted(key, store_dir=root):
            stamped += 1
    return stamped


def _recap_key(tags: list[str]) -> str | None:
    """`<source>:<conversation_id>` from a recap's own tags, or None.

    A record written before either tag existed, or by hand, has no key to
    check and is left alone rather than guessed at.
    """
    source = next((t[len("source:"):] for t in tags if t.startswith("source:")), "")
    chat = next((t[len("chat:"):] for t in tags if t.startswith("chat:")), "")
    return f"{source}:{chat}" if source and chat else None


class WorkIndexSweepJob(BaseJob):
    """Prune orphan rows from the derived indexes, and stamp orphan recaps."""

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        cm_pruned = 0
        wi_pruned = 0
        recaps_stamped = 0
        errors: list[str] = []

        home = _home()
        # Three disk walks that share nothing — off the loop, and together.
        cm_result, wi_result, recap_result = await asyncio.gather(
            asyncio.to_thread(_prune_chat_metadata_sync, home),
            asyncio.to_thread(_prune_work_index_sync, home),
            asyncio.to_thread(_stamp_orphan_recaps_sync, home),
            return_exceptions=True,
        )
        if isinstance(cm_result, BaseException):
            log.error("work_index_sweep: chat_metadata prune failed", exc_info=cm_result)
            errors.append(f"chat_metadata: {cm_result!r}")
        else:
            cm_pruned = cm_result

        if isinstance(wi_result, BaseException):
            log.error("work_index_sweep: work_index prune failed", exc_info=wi_result)
            errors.append(f"work_index: {wi_result!r}")
        else:
            wi_pruned = wi_result

        if isinstance(recap_result, BaseException):
            log.error("work_index_sweep: recap stamp failed", exc_info=recap_result)
            errors.append(f"recaps: {recap_result!r}")
        else:
            recaps_stamped = recap_result

        ok = not errors
        detail_parts = [
            f"chat_metadata_pruned={cm_pruned}",
            f"work_index_paths_pruned={wi_pruned}",
            f"recaps_stamped={recaps_stamped}",
        ]
        if errors:
            detail_parts.append("errors=" + "; ".join(errors))
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=ok,
            detail=" ".join(detail_parts),
            payload={
                "chat_metadata_pruned": cm_pruned,
                "work_index_paths_pruned": wi_pruned,
                "recaps_stamped": recaps_stamped,
                "errors": errors,
            },
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )
