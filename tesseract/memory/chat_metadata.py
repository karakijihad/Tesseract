"""SQLite derived index over the chat records — the drawer's day view, fast.

Same role as ``tesseract/memory/fts_index.py`` and ``work_index.py``: the
canonical state is ``sessions/chats/<chat_id>.json``, this is derived from it,
and it is always rebuildable — ``chat_store.rebuild_metadata_index()``.

Why: ``chat_store.list_by_day`` loads every chat to read six header fields, and
``/api/chats/days`` is hit on every drawer open, so the cost grows with the
operator's whole history rather than with what is shown. One query returns the
same rows.

Schema (one row per chat record)::

    chat_id     TEXT PRIMARY KEY   -- uuid4 hex, assigned once at creation
    title       TEXT
    created_at  TEXT NOT NULL      -- ISO-8601, stamped once; the day comes from here
    started_at  TEXT
    ended_at    TEXT
    turn_count  INTEGER
    model       TEXT
    archived    INTEGER            -- 0/1
    file_path   TEXT NOT NULL      -- absolute path on disk

Three things the retiring session index carried are gone with the filename they
were parsed out of. ``date_prefix`` and its ``custom`` bucket: a uuid names no
date, so there is nothing to fall back from and ``created_at`` answers every
time. ``archived_in``: the month came from the archive FOLDER a file was moved
into, and a chat record never moves — archived is a flag, and "archived in
which month" is ``ended_at``. And ``session_id`` is not carried at all: it is
minted per WebSocket connection and resolves to nothing.

This module knows nothing about the chat store — the record's owner builds the
rows and feeds them in. The index has no directory walk of its own: one owner
of the records, rather than one reader per consumer.

Index covers ``created_at`` for the per-day grouping and ``archived`` for the
drawer's archive view. WAL enables concurrent readers + one writer (same as
``WorkIndex``).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatMetaRow:
    chat_id: str
    title: str
    created_at: str
    started_at: str
    ended_at: str | None
    turn_count: int
    model: str
    archived: bool
    file_path: str


class ChatMetadataIndex:
    """SQLite-backed derived index over the chat corpus."""

    def __init__(self, db_path: Path | str) -> None:
        if isinstance(db_path, str) and db_path == ":memory:":
            self._db_path = Path(":memory:")
            self._conn = sqlite3.connect(":memory:")
        else:
            self._db_path = Path(db_path)
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._deferred = False
        self._create_schema()

    @contextmanager
    def deferred(self) -> Iterator[None]:
        """Hold the writes made inside in one transaction, committed on exit.

        A commit is an fsync, and the autosave pump upserts every open chat on
        one tick — per-write that is the disk cost times however many
        conversations the operator has open, on the event loop. On an
        exception the block is left uncommitted and the caller's close rolls
        it back: the index is derived, so losing one tick's rows costs a
        rebuild, while a half-written burst would need noticing first.
        """
        self._deferred = True
        try:
            yield
            self._conn.commit()
        finally:
            self._deferred = False

    def _commit(self) -> None:
        if not self._deferred:
            self._conn.commit()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_metadata (
                chat_id     TEXT PRIMARY KEY,
                title       TEXT,
                created_at  TEXT NOT NULL,
                started_at  TEXT,
                ended_at    TEXT,
                turn_count  INTEGER NOT NULL DEFAULT 0,
                model       TEXT,
                archived    INTEGER NOT NULL DEFAULT 0,
                file_path   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_meta_created
                ON chat_metadata(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_chat_meta_archived
                ON chat_metadata(archived);
            """
        )
        self._conn.commit()

    # ── write paths ──────────────────────────────────────────────────

    def upsert(self, row: ChatMetaRow) -> None:
        try:
            self._conn.execute(_UPSERT_SQL, _values(row))
            self._commit()
        except Exception:
            logger.warning("chat_metadata upsert failed for %s", row.chat_id)

    def delete(self, chat_id: str) -> None:
        try:
            self._conn.execute(
                "DELETE FROM chat_metadata WHERE chat_id = ?", (chat_id,)
            )
            self._commit()
        except Exception:
            logger.warning("chat_metadata delete failed for %s", chat_id)

    def replace_all(self, rows: Iterable[ChatMetaRow]) -> int:
        """Drop every row and insert these. Returns the count written.

        One transaction, so a reader never sees an empty index between the
        delete and the insert — the day view falls back to the disk walk on an
        empty read, and a rebuild is exactly when that walk is most expensive.
        """
        try:
            with self._conn:
                self._conn.execute("DELETE FROM chat_metadata")
                written = 0
                for row in rows:
                    self._conn.execute(_UPSERT_SQL, _values(row))
                    written += 1
            return written
        except Exception:
            logger.exception("chat_metadata rebuild failed")
            return 0

    # ── read paths ───────────────────────────────────────────────────

    def list_headers(
        self, *, include_archived: bool = False, archived_only: bool = False
    ) -> list[dict[str, Any]]:
        """The header fields for each chat, newest-created first.

        The grouping into days lives in ``chat_store`` and runs over these rows
        or over parsed records interchangeably — the retiring index re-derived
        the day view here instead, and its sort key had to be kept "matching"
        a copy in another module by comment.
        """
        sql = (
            "SELECT chat_id, title, created_at, started_at, ended_at, "
            "turn_count, model, archived FROM chat_metadata"
        )
        if archived_only:
            sql += " WHERE archived = 1"
        elif not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY created_at DESC"
        try:
            cursor = self._conn.execute(sql)
        except sqlite3.OperationalError:
            return []
        return [
            {
                "chat_id": chat_id,
                "title": title or "",
                "created_at": created_at,
                "started_at": started_at,
                "ended_at": ended_at,
                "turn_count": int(turn_count or 0),
                "model": model or "",
                "archived": bool(archived),
            }
            for (
                chat_id, title, created_at, started_at, ended_at,
                turn_count, model, archived,
            ) in cursor
        ]

    def count(self) -> int:
        try:
            cursor = self._conn.execute("SELECT COUNT(*) FROM chat_metadata")
            return int(cursor.fetchone()[0])
        except Exception:
            return 0

    # ── maintenance ──────────────────────────────────────────────────

    def prune_orphans(self) -> int:
        """Drop rows whose ``file_path`` no longer exists on disk.

        ``chat_store.delete_chat`` writes through, so this is the backstop for
        a delete that bypassed the tool — an operator ``rm`` from a shell, an
        external sync — not the normal path.
        """
        try:
            cursor = self._conn.execute(
                "SELECT chat_id, file_path FROM chat_metadata"
            )
        except sqlite3.OperationalError:
            return 0
        dropped = 0
        for chat_id, path in list(cursor):
            try:
                if not Path(path).exists():
                    self._conn.execute(
                        "DELETE FROM chat_metadata WHERE chat_id = ?", (chat_id,)
                    )
                    dropped += 1
            except Exception:
                continue
        if dropped:
            self._conn.commit()
        return dropped

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_UPSERT_SQL = """
    INSERT INTO chat_metadata
        (chat_id, title, created_at, started_at, ended_at,
         turn_count, model, archived, file_path)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id) DO UPDATE SET
        title      = excluded.title,
        created_at = excluded.created_at,
        started_at = excluded.started_at,
        ended_at   = excluded.ended_at,
        turn_count = excluded.turn_count,
        model      = excluded.model,
        archived   = excluded.archived,
        file_path  = excluded.file_path
"""


def _values(row: ChatMetaRow) -> tuple[Any, ...]:
    return (
        row.chat_id,
        row.title or "",
        row.created_at,
        row.started_at,
        row.ended_at,
        int(row.turn_count or 0),
        row.model or "",
        1 if row.archived else 0,
        row.file_path,
    )
