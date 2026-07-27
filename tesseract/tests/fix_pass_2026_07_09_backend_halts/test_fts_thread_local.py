"""2026-07-09 backend-halt diagnosis — FTS cross-thread SQLite.

Live log: `FTS rebuild failed: SQLite objects created in a thread can only
be used in that same thread. The object was created in thread id X and this
is thread id Y.` `FTSIndex` opened a single connection in `__init__` (boot
thread) and `rebuild()` runs under `asyncio.to_thread` (pool thread) — the
cross-thread use raises. `_with_recovery` masked it by reconnecting on the
pool thread, but that bounced connection ownership between threads, so every
alternating main-thread `search()` then failed-and-reconnected — constant
churn + a WARNING per turn.

Fix: per-thread (thread-local) connections. Each thread lazily opens its own
connection to the same WAL database; no connection is ever used off the
thread that created it.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tesseract.memory import fts_index as fts_index_module
from tesseract.memory.fts_index import FTSIndex


def test_rebuild_from_another_thread_does_not_crosstalk(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """RED (pre-fix): `rebuild()` on a pool thread hits the boot-thread
    connection → cross-thread ProgrammingError → a `FTS rebuild failed`
    WARNING (masked by reconnect). GREEN (post-fix): the pool thread uses
    its own connection, rebuild succeeds cleanly with no failure log, and
    the main-thread connection is untouched so `search()` still works.
    """
    fts = FTSIndex(db_path=tmp_path / "fts.db")
    fts.add("mem_seed", "Seed", "seed body for the main-thread connection")

    memories = [
        ("mem_a", "Alpha record", "The alpha record body text for BM25 indexing."),
        ("mem_b", "Beta record", "The beta record body text for BM25 indexing."),
    ]

    caplog.set_level(logging.DEBUG, logger=fts_index_module.__name__)
    with ThreadPoolExecutor(max_workers=1) as pool:
        count = pool.submit(fts.rebuild, memories).result()

    assert count == len(memories)
    assert not any(
        "rebuild failed" in r.message or "same thread" in r.message
        for r in caplog.records
    ), "rebuild on a pool thread must not trigger a cross-thread failure"

    # Main-thread connection must still be usable straight after — pre-fix it
    # now pointed at the pool thread's connection and would itself fail first.
    results = fts.search("alpha")
    assert any(mem_id == "mem_a" for mem_id, _ in results)


def test_rebuild_larger_than_commit_batch_is_correct(tmp_path: Path) -> None:
    """Rebuild commits in batches to release the writer lock; a set larger
    than one batch must still index every row and stay searchable."""
    fts = FTSIndex(db_path=tmp_path / "fts.db")
    n = fts_index_module._REBUILD_COMMIT_BATCH * 2 + 5
    memories = [
        (f"mem_{i}", f"Title {i}", f"batched body token{i} for BM25 indexing")
        for i in range(n)
    ]
    assert fts.rebuild(memories) == n
    assert fts.all_ids() == {f"mem_{i}" for i in range(n)}
    results = fts.search("token7")
    assert any(mem_id == "mem_7" for mem_id, _ in results)


def test_search_across_threads_is_consistent(tmp_path: Path) -> None:
    """A write on one thread is visible to a read on another (same WAL db)."""
    fts = FTSIndex(db_path=tmp_path / "fts.db")
    fts.add("mem_x", "Xray", "xray body text for BM25 indexing across threads")

    with ThreadPoolExecutor(max_workers=1) as pool:
        results = pool.submit(fts.search, "xray").result()

    assert any(mem_id == "mem_x" for mem_id, _ in results)
