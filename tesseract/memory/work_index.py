"""SQLite + FTS5 index over session transcripts and workshop artifacts.

CR-1 (2026-05-22) — non-authoritative retrieval surface that complements
the memory store. Chunks carry ``source`` ∈ {``"session"``, ``"workshop"``}
provenance; callers (the ``recall_history`` tool, the future merged
retrieval pipeline) must label results so the operator and the model
can tell the difference between promoted facts (memory) and work
history (suggestions).

This module is intentionally separate from ``tesseract/memory/fts_index.py``
(which is dedicated to the memory store) — same FTS5 + WAL pattern,
but the schema and ranking knobs are domain-specific.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkChunk:
    """A single retrievable unit. One row in the FTS table.

    ``source`` controls provenance label in retrieval; ``source_path``
    is the on-disk file the chunk came from so the model can ``file_read``
    for full context; ``source_ref`` is a stable short id (session id
    for sessions, path-relative slug for workshop files); ``turn_idx``
    and ``role`` are session-only metadata (None for workshop chunks);
    ``chunk_idx`` is 0-based within the source file; ``ts`` is ISO-8601.
    """
    source: str
    source_path: str
    source_ref: str
    turn_idx: int | None
    role: str | None
    chunk_idx: int
    ts: str
    text: str


@dataclass(frozen=True)
class WorkHit(WorkChunk):
    """A search result — same shape as ``WorkChunk`` plus a relevance score."""
    score: float


class WorkIndex:
    """SQLite + FTS5 work-history index.

    Single FTS5 virtual table — all columns indexed; ``text`` carries
    the searchable body, the others are filters. ``rank`` is BM25 (lower
    = better match in SQLite; we negate to return positive scores).
    """

    def __init__(self, db_path: Path | str) -> None:
        # Connections are THREAD-LOCAL for file-backed DBs (same 2026-07-09
        # backend-halt pattern as `fts_index.FTSIndex`): a `sqlite3.Connection`
        # may only be used on its creating thread — cross-thread use raises
        # ProgrammingError and silently drops results (trio W0 audit D6). This
        # keeps the index safe under any pool-thread caller (`asyncio.to_thread`).
        # NOTE (2026-07-18 audit): `retrieve()`'s work-history search currently
        # runs ON the event loop (`_fetch_work_history` → `search`), not via a
        # pool thread — the BM25 lookup is a cheap indexed query, so it is left
        # on-loop deliberately; the thread-local design remains for correctness
        # if any caller ever offloads it. `:memory:` keeps a single shared
        # connection: per-thread `:memory:` connections would be separate empty
        # DBs (single-thread test convenience only).
        self._memory_conn: sqlite3.Connection | None = None
        if isinstance(db_path, str) and db_path == ":memory:":
            self._db_path = Path(":memory:")
            self._memory_conn = sqlite3.connect(":memory:")
            self._memory_conn.execute("PRAGMA journal_mode=WAL")
        else:
            self._db_path = Path(db_path)
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Touch the boot-thread connection eagerly so the FTS5 table exists
        # before the first query, matching the previous constructor contract.
        self._create_table()

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def _create_table(self) -> None:
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS work_chunks "
            "USING fts5("
            "source, source_path, source_ref, turn_idx UNINDEXED, "
            "role, chunk_idx UNINDEXED, ts, text, "
            "tokenize='porter unicode61')"
        )
        self._conn.commit()

    def add(self, chunk: WorkChunk) -> None:
        try:
            self._conn.execute(
                "INSERT INTO work_chunks(source, source_path, source_ref, "
                "turn_idx, role, chunk_idx, ts, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk.source,
                    chunk.source_path,
                    chunk.source_ref,
                    "" if chunk.turn_idx is None else str(chunk.turn_idx),
                    chunk.role or "",
                    str(chunk.chunk_idx),
                    chunk.ts,
                    chunk.text,
                ),
            )
            self._conn.commit()
        except Exception:
            logger.warning("work_index add failed for %s/%s",
                           chunk.source, chunk.source_ref)

    def delete_by_path(self, source_path: str) -> None:
        """Remove every chunk associated with ``source_path``.

        Used by re-ingest paths so the next ``add`` doesn't double-count.
        """
        try:
            self._conn.execute(
                "DELETE FROM work_chunks WHERE source_path = ?",
                (source_path,),
            )
            self._conn.commit()
        except Exception:
            logger.warning("work_index delete failed for %s", source_path)

    def search(
        self,
        query: str,
        *,
        source: str = "both",
        since: str | None = None,
        until: str | None = None,
        top_k: int = 5,
    ) -> list[WorkHit]:
        """Return BM25-ranked hits matching ``query``.

        ``source`` ∈ {``"session"``, ``"workshop"``, ``"both"``} filters
        the result set. ``since`` / ``until`` are ISO-8601 prefix bounds
        applied lexicographically against ``ts`` (works because ISO-8601
        sorts correctly as strings).
        """
        if not query.strip():
            return []
        safe = _sanitize_query(query)
        if not safe:
            return []
        sql = (
            "SELECT source, source_path, source_ref, turn_idx, role, "
            "chunk_idx, ts, text, rank FROM work_chunks "
            "WHERE work_chunks MATCH ?"
        )
        params: list[object] = [safe]
        if source in ("session", "workshop"):
            sql += " AND source = ?"
            params.append(source)
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        if until:
            sql += " AND ts <= ?"
            params.append(until)
        sql += " ORDER BY rank LIMIT ?"
        params.append(int(top_k))
        try:
            cursor = self._conn.execute(sql, params)
        except sqlite3.OperationalError:
            logger.warning("work_index search failed for query: %s", query[:80])
            return []
        out: list[WorkHit] = []
        for row in cursor:
            s, path, ref, turn, role, chunk, ts, text, rank = row
            out.append(WorkHit(
                source=s,
                source_path=path,
                source_ref=ref,
                turn_idx=int(turn) if turn else None,
                role=role or None,
                chunk_idx=int(chunk) if chunk else 0,
                ts=ts,
                text=text,
                score=-float(rank),
            ))
        return out

    def prune_orphans(self) -> int:
        """Drop every chunk whose ``source_path`` no longer exists on disk.

        Mirrors :meth:`ChatMetadataIndex.prune_orphans` — the daily
        sweep calls both. Walks DISTINCT source_paths (one stat per
        file, not per chunk), then deletes by path. Files canonical;
        the index catches up to disk truth without a full rebuild.
        Returns the number of paths whose chunks were dropped.
        """
        try:
            cursor = self._conn.execute(
                "SELECT DISTINCT source_path FROM work_chunks"
            )
        except sqlite3.OperationalError:
            return 0
        paths = [row[0] for row in cursor]
        dropped_paths = 0
        for path in paths:
            try:
                if path and not Path(path).exists():
                    self.delete_by_path(path)
                    dropped_paths += 1
            except Exception:  # noqa: BLE001
                continue
        return dropped_paths

    def count(self) -> int:
        try:
            cursor = self._conn.execute("SELECT COUNT(*) FROM work_chunks")
            return int(cursor.fetchone()[0])
        except Exception:
            return 0

    def rebuild(self, chunks: Iterable[WorkChunk]) -> int:
        """Drop all rows and re-populate from ``chunks``. Returns count."""
        try:
            self._conn.execute("DELETE FROM work_chunks")
            n = 0
            for c in chunks:
                self.add(c)
                n += 1
            return n
        except Exception:
            logger.exception("work_index rebuild failed")
            return 0

    def close(self) -> None:
        """Close this thread's connection (and the shared `:memory:` one).
        Other threads' connections close when their threads exit."""
        try:
            self._conn.close()
        except Exception:
            pass
        if self._memory_conn is None:
            self._local.conn = None


def _sanitize_query(query: str) -> str:
    """Quote each whitespace token as an FTS5 string literal.

    Stripping punctuation (the prior approach) was lossy and left FTS5
    boolean keywords live: ``foo-bar`` split words, ``What's`` dropped the
    contraction, and ``plate AND now`` parsed ``AND`` as a boolean operator
    (silently dropping most matches). Wrapping each token in double quotes
    (internal quotes doubled) makes every token an inert string literal —
    apostrophes, hyphens, ``&``, and keyword-words all match literally.

    Tokens are joined with a space (implicit FTS5 AND) — work-history recall
    stays precise (all terms present), matching the prior semantics.
    """
    tokens = [
        '"' + word.replace('"', '""') + '"'
        for word in query.split()
        if word
    ]
    return " ".join(tokens)
