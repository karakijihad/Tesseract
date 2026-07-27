"""RC2 regression — P7 live-gate diagnosis (memory-retrieval-diagnosis.md).

`FTSIndex` is a single long-lived connection with no rollback on failure
and a blanket `except Exception` that hides the real error. Once one bad
transaction poisons `self._conn`, every later call on the same instance
keeps failing — `rebuild()` returns 0 forever, `search()` returns []
forever — until the process restarts.

Fix: `_with_recovery` rolls back, logs the real exception (loud once,
then quiet), reconnects, and retries once. Covers:
- a transaction poisoned mid-`rebuild()` recovers within the same call,
  and a subsequent `search()` on the same instance still works.
- the real exception is logged (not swallowed blind).
- repeated failures against a permanently broken connection log WARNING
  once, then downgrade to DEBUG (no per-turn log spam).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from tesseract.memory import fts_index as fts_index_module
from tesseract.memory.fts_index import FTSIndex


class _PoisonedConn:
    """Wraps a real sqlite3.Connection; raises on `execute()` from the
    `fail_after`-th call onward (permanently, unless replaced)."""

    def __init__(self, real_conn: sqlite3.Connection, fail_after: int) -> None:
        self._real = real_conn
        self._calls = 0
        self._fail_after = fail_after

    def execute(self, *args, **kwargs):
        self._calls += 1
        if self._calls > self._fail_after:
            raise sqlite3.OperationalError("simulated poisoned connection")
        return self._real.execute(*args, **kwargs)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        return self._real.close()


def test_rebuild_recovers_from_mid_transaction_poisoning(tmp_path: Path) -> None:
    """RED (pre-fix): the 3rd `execute()` call inside `rebuild()` raises;
    the old code has no rollback/reconnect, so it logs, returns 0, and
    leaves `self._conn` permanently broken — every later `search()` on
    this instance also fails.

    GREEN (post-fix): `rebuild()` rolls back, reconnects to a fresh real
    connection, and retries — succeeding within the same call. A
    subsequent `search()` on the same instance also works.
    """
    fts = FTSIndex(db_path=tmp_path / "fts.db")
    # Poison AFTER init — _create_table already ran on the real connection.
    fts._conn = _PoisonedConn(fts._conn, fail_after=2)

    memories = [
        ("mem_a", "Alpha record", "The alpha record body text for BM25 indexing."),
        ("mem_b", "Beta record", "The beta record body text for BM25 indexing."),
    ]
    count = fts.rebuild(memories)

    assert count == len(memories), "rebuild must recover and insert everything, not return 0"

    results = fts.search("alpha")
    assert any(mem_id == "mem_a" for mem_id, _score in results), (
        "search() on the same instance must still work after rebuild() recovered"
    )


def test_search_recovers_after_poisoning_without_prior_rebuild(tmp_path: Path) -> None:
    """A connection poisoned before any rebuild still lets `search()`
    recover via reconnect-and-retry, rather than staying broken forever."""
    fts = FTSIndex(db_path=tmp_path / "fts.db")
    fts.add("mem_c", "Gamma record", "The gamma record body text for BM25 indexing.")

    # Poison after the add() succeeded — search() must still recover.
    fts._conn = _PoisonedConn(fts._conn, fail_after=0)

    results = fts.search("gamma")
    assert any(mem_id == "mem_c" for mem_id, _score in results)


def test_real_exception_is_logged_not_swallowed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The actual exception repr must reach the log, not just a generic
    'FTS search failed' message with no diagnostic detail."""
    fts = FTSIndex(db_path=tmp_path / "fts.db")
    fts._conn = _PoisonedConn(fts._conn, fail_after=0)

    caplog.set_level(logging.DEBUG, logger=fts_index_module.__name__)
    fts.search("some query")

    assert any("simulated poisoned connection" in r.message for r in caplog.records), (
        "the real exception's repr must appear in the log output"
    )


def test_repeated_failures_downgrade_to_debug_after_first_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A permanently broken connection (reconnect itself also fails) must
    log the first failure at WARNING and every subsequent failure at
    DEBUG — no per-turn WARNING spam."""
    fts = FTSIndex(db_path=tmp_path / "fts.db")
    fts._conn = _PoisonedConn(fts._conn, fail_after=0)
    monkeypatch.setattr(
        fts_index_module.sqlite3, "connect",
        lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("disk unavailable")),
    )

    caplog.set_level(logging.DEBUG, logger=fts_index_module.__name__)
    fts.search("first query")
    fts.search("second query")
    fts.search("third query")

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warning_records) == 1, f"expected exactly one WARNING, got {len(warning_records)}"
    assert len(debug_records) >= 1, "subsequent failures must downgrade to DEBUG"
