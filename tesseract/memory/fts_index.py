"""SQLite FTS5 full-text index for BM25 keyword search.

Provides keyword-based retrieval as the BM25 half of hybrid search.
Graceful failure: all public methods return empty on error.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# `rebuild()` commits every this-many inserts so its writer lock is released
# frequently — bounds how long a concurrent loop-thread FTS write can block
# on the shared SQLite writer lock (see `rebuild` docstring, 2026-07-09).
_REBUILD_COMMIT_BATCH = 200


class FTSIndex:
    """BM25 keyword index over a WAL SQLite FTS5 table.

    Connections are **thread-local**. `search()`/`add()` run on the event
    loop's thread while `rebuild()` runs under `asyncio.to_thread` on a pool
    thread; a `sqlite3.Connection` may only be used from the thread that
    created it. Each thread lazily opens its own connection to the same WAL
    database (2026-07-09 backend-halt fix). `_conn` is a per-thread view so
    the recovery path and existing tests keep working unchanged.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Open the boot-thread connection eagerly so the FTS5 table exists
        # before the first query, matching the previous constructor contract.
        self._new_conn()
        # RC2 — first-failure-loud, then-quiet logging. A poisoned
        # connection retries every call; without this the same exception
        # would log at WARNING on every single turn.
        self._logged_failure = False

    def _new_conn(self) -> sqlite3.Connection:
        """Open a fresh connection for the current thread, WAL-enable it,
        ensure the FTS5 table exists, and stash it on the thread-local."""
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        self._local.conn = conn
        self._create_table()
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
        return conn

    @_conn.setter
    def _conn(self, value: sqlite3.Connection) -> None:
        # Preserves the `fts._conn = <conn>` assignment used by `_reconnect`
        # and the RC2 resilience tests — scoped to the current thread.
        self._local.conn = value

    def _reconnect(self) -> None:
        """Drop and reopen this thread's connection. Recovery path for RC2:
        a bad transaction can leave the connection unusable for the rest of
        the process lifetime; reopening clears that state so the NEXT call
        (not just the one that failed) can succeed again."""
        try:
            self._conn.close()
        except Exception:
            pass
        self._new_conn()

    def _log_failure(self, action: str, exc: Exception, detail: str = "") -> None:
        """Log the real exception once at WARNING, then downgrade repeats
        to DEBUG so a poisoned connection doesn't spam the log every turn."""
        level = logging.WARNING if not self._logged_failure else logging.DEBUG
        self._logged_failure = True
        logger.log(level, "FTS %s failed%s: %r", action, f" ({detail})" if detail else "", exc)

    def _with_recovery(self, action: str, fn, on_failure, detail: str = ""):
        """Run `fn()` against `self._conn`. On exception: roll back, log the
        real exception (RC2 — no more blind `except Exception: pass`),
        reconnect, and retry once against the fresh connection. Returns
        `fn()`'s result, or `on_failure` if the retry also fails — so a
        single bad transaction can no longer poison every later call on
        this instance until process restart.
        """
        try:
            result = fn()
            self._logged_failure = False
            return result
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self._log_failure(action, exc, detail)
        try:
            self._reconnect()
            result = fn()
            self._logged_failure = False
            return result
        except Exception as exc2:
            self._log_failure(action, exc2, detail)
            return on_failure

    def _create_table(self) -> None:
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories "
            "USING fts5(memory_id, title, body, tokenize='porter unicode61')"
        )
        self._conn.commit()

    def add(self, memory_id: str, title: str, body: str) -> None:
        def _do() -> None:
            self._conn.execute(
                "DELETE FROM memories WHERE memory_id = ?", (memory_id,)
            )
            self._conn.execute(
                "INSERT INTO memories(memory_id, title, body) VALUES (?, ?, ?)",
                (memory_id, title, body),
            )
            self._conn.commit()

        self._with_recovery("add", _do, None, memory_id)

    def delete(self, memory_id: str) -> None:
        def _do() -> None:
            self._conn.execute(
                "DELETE FROM memories WHERE memory_id = ?", (memory_id,)
            )
            self._conn.commit()

        self._with_recovery("delete", _do, None, memory_id)

    def search(
        self,
        query: str,
        filter_ids: set[str] | None = None,
        limit: int = 20,
        require_prefix: str | None = None,
    ) -> list[tuple[str, float]]:
        """Search by BM25. Returns [(memory_id, score)] sorted best-first.

        BM25 scores are negative in SQLite (lower = better match),
        so we negate them to return positive scores (higher = better).

        `require_prefix` filters SQL-side. The table is shared between
        memory records (mem_*), vault chunks (vault:*) and daily notes
        (daily_*) — without the filter, foreign rows crowd the LIMIT
        window and the caller's own hits never come back at all.
        """
        if not query.strip():
            return []
        safe_query = self._sanitize_query(query)
        if not safe_query:
            return []

        def _do() -> list[tuple[str, float]]:
            sql = "SELECT memory_id, rank FROM memories WHERE memories MATCH ? "
            params: list = [safe_query]
            if require_prefix:
                sql += "AND memory_id LIKE ? ESCAPE '\\' "
                params.append(self._escape_like(require_prefix) + "%")
            sql += "ORDER BY rank LIMIT ?"
            params.append(limit * 3 if filter_ids else limit)
            cursor = self._conn.execute(sql, params)
            results = []
            for row in cursor:
                mem_id, rank = row
                if filter_ids is not None and mem_id not in filter_ids:
                    continue
                results.append((mem_id, -rank))
                if len(results) >= limit:
                    break
            return results

        return self._with_recovery("search", _do, [], query[:50])

    def rebuild(
        self,
        memories: list[tuple[str, str, str]],
        preserve_prefixes: tuple[str, ...] = (),
    ) -> int:
        """Rebuild index from scratch. Takes [(memory_id, title, body)]. Returns count.

        The table is shared with rows the caller cannot re-feed (vault chunks,
        daily notes); `preserve_prefixes` keeps rows whose memory_id starts
        with any given prefix instead of deleting them.

        Commits in batches. `rebuild()` runs off the event loop (via
        `asyncio.to_thread` in `index_rebuild.py`) on its own thread-local
        connection, so it is genuinely concurrent with loop-thread writers
        (`add`/`delete` from memory_save, consistency, vault_indexer — none
        offloaded). One long write transaction would hold SQLite's single
        writer lock for the whole rebuild, blocking those loop-thread writes
        (and thus Mirror/Telegram) for its full duration. Committing every
        `_REBUILD_COMMIT_BATCH` rows releases the lock frequently so a
        colliding loop-thread write waits at most one small batch."""
        def _do() -> int:
            if preserve_prefixes:
                conditions = " AND ".join(
                    "memory_id NOT LIKE ? ESCAPE '\\'" for _ in preserve_prefixes
                )
                params = [self._escape_like(p) + "%" for p in preserve_prefixes]
                self._conn.execute(f"DELETE FROM memories WHERE {conditions}", params)
            else:
                self._conn.execute("DELETE FROM memories")
            self._conn.commit()
            count = 0
            for memory_id, title, body in memories:
                self._conn.execute(
                    "INSERT INTO memories(memory_id, title, body) VALUES (?, ?, ?)",
                    (memory_id, title, body),
                )
                count += 1
                if count % _REBUILD_COMMIT_BATCH == 0:
                    self._conn.commit()
            self._conn.commit()
            return count

        return self._with_recovery("rebuild", _do, 0)

    def get_body(self, memory_id: str) -> str | None:
        """Stored body text for one row, or None. Feeds the reranker's
        (query, text) pairs without touching the filesystem."""
        def _do() -> str | None:
            cursor = self._conn.execute(
                "SELECT body FROM memories WHERE memory_id = ?", (memory_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

        return self._with_recovery("get_body", _do, None, memory_id)

    def all_ids(self) -> set[str]:
        try:
            cursor = self._conn.execute("SELECT memory_id FROM memories")
            return {row[0] for row in cursor}
        except Exception:
            return set()

    def reindex_file(self, memory_id: str, title: str, body: str) -> None:
        self.add(memory_id, title, body)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _escape_like(prefix: str) -> str:
        """Escape LIKE wildcards so a prefix matches literally."""
        return (
            prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )

    @staticmethod
    def _sanitize_query(query: str) -> str:
        """Quote each token as an FTS5 string literal to prevent syntax errors."""
        words = []
        for word in query.split():
            if not word:
                continue
            words.append('"' + word.replace('"', '""') + '"')
        return " OR ".join(words) if words else ""
