"""W1 D6a — WorkIndex must be usable across threads (`retrieve()` calls it
from `asyncio.to_thread` pool threads; pre-fix any cross-thread call raised
sqlite3.ProgrammingError and work-history silently vanished)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from tesseract.memory.work_index import WorkChunk, WorkIndex


def _chunk(text: str, path: str = "x/session.jsonl") -> WorkChunk:
    return WorkChunk(
        source="session",
        source_path=path,
        source_ref="2026-07-09-abcd1234",
        turn_idx=0,
        role="user",
        chunk_idx=0,
        ts="2026-07-09T12:00:00+00:00",
        text=text,
    )


def test_search_from_worker_thread(tmp_path):
    """Regression: created on main thread, searched from a pool thread."""
    idx = WorkIndex(tmp_path / "work.db")
    idx.add(_chunk("the trio verify loop relays coder and auditor"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        hits = pool.submit(idx.search, "trio verify loop").result()
    assert len(hits) == 1
    assert "trio" in hits[0].text


def test_write_from_worker_thread_visible_on_main(tmp_path):
    idx = WorkIndex(tmp_path / "work.db")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(idx.add, _chunk("written off the main thread")).result()
    hits = idx.search("written main thread")
    assert len(hits) == 1


def test_two_distinct_worker_threads(tmp_path):
    """Each pool thread gets its own connection to the same WAL db."""
    idx = WorkIndex(tmp_path / "work.db")
    idx.add(_chunk("alpha beta gamma"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        r1 = pool.submit(idx.search, "alpha").result()
        r2 = pool.submit(idx.search, "gamma").result()
    assert len(r1) == 1 and len(r2) == 1


def test_memory_db_still_works_single_thread():
    idx = WorkIndex(":memory:")
    idx.add(_chunk("in memory chunk"))
    assert len(idx.search("memory chunk")) == 1
    assert idx.count() == 1
