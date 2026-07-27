"""SQLite derived index for session metadata — fast Mirror drawer listing.

Same role as ``tesseract/memory/fts_index.py`` and ``work_index.py``:
the canonical state lives in JSON files under ``tesseract/sessions/``,
this is a derived index populated by the mutators in
``tesseract/brain/session_store.py``. Always rebuildable from disk via
``rebuild_from_disk``.

Why: the Mirror drawer's session-list path used to open every JSON
file on every render. At ~100 sessions that's ~30-50 ms; at archival
scale it grows linearly. One SQLite query returns the same rows in
sub-millisecond.

Schema (one row per session):

    session_id   TEXT PRIMARY KEY   -- file stem
    started_at   TEXT NOT NULL      -- ISO-8601
    ended_at     TEXT               -- ISO-8601 or NULL
    turn_count   INTEGER            -- # user turns
    model        TEXT               -- whatever the session recorded
    date_prefix  TEXT               -- 'YYYY-MM-DD' or 'custom'
    archived_in  TEXT               -- 'YYYY-MM' when archived, NULL when active
    file_path    TEXT NOT NULL      -- absolute path on disk

Index covers ``date_prefix, started_at`` for the per-day grouping
and ``archived_in`` for the archive route. WAL enables concurrent
readers + one writer (same as ``WorkIndex``).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Matches the per-run filename pattern from `session_store._DATE_PREFIX_RE`.
# Custom-named sessions (operator-chosen) land in a synthetic `custom` bucket.
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-\d{4})?$")


def _date_prefix(session_id: str) -> str:
    m = _DATE_PREFIX_RE.match(session_id)
    return m.group(1) if m else "custom"


@dataclass(frozen=True)
class SessionMetaRow:
    session_id: str
    started_at: str
    ended_at: str | None
    turn_count: int
    model: str
    file_path: str
    archived_in: str | None = None


class SessionMetadataIndex:
    """SQLite-backed derived index over the session corpus."""

    def __init__(self, db_path: Path | str) -> None:
        if isinstance(db_path, str) and db_path == ":memory:":
            self._db_path = Path(":memory:")
            self._conn = sqlite3.connect(":memory:")
        else:
            self._db_path = Path(db_path)
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS session_metadata (
                session_id   TEXT PRIMARY KEY,
                started_at   TEXT NOT NULL,
                ended_at     TEXT,
                turn_count   INTEGER NOT NULL DEFAULT 0,
                model        TEXT,
                date_prefix  TEXT NOT NULL,
                archived_in  TEXT,
                file_path    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_session_meta_date
                ON session_metadata(date_prefix, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_session_meta_archived
                ON session_metadata(archived_in);
            """
        )
        self._conn.commit()

    # ── write paths ──────────────────────────────────────────────────

    def upsert(self, row: SessionMetaRow) -> None:
        try:
            self._conn.execute(
                """
                INSERT INTO session_metadata
                    (session_id, started_at, ended_at, turn_count, model,
                     date_prefix, archived_in, file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    started_at = excluded.started_at,
                    ended_at   = excluded.ended_at,
                    turn_count = excluded.turn_count,
                    model      = excluded.model,
                    date_prefix = excluded.date_prefix,
                    archived_in = excluded.archived_in,
                    file_path  = excluded.file_path
                """,
                (
                    row.session_id,
                    row.started_at,
                    row.ended_at,
                    int(row.turn_count or 0),
                    row.model or "",
                    _date_prefix(row.session_id),
                    row.archived_in,
                    row.file_path,
                ),
            )
            self._conn.commit()
        except Exception:
            logger.warning("session_metadata upsert failed for %s", row.session_id)

    def delete(self, session_id: str) -> None:
        try:
            self._conn.execute(
                "DELETE FROM session_metadata WHERE session_id = ?",
                (session_id,),
            )
            self._conn.commit()
        except Exception:
            logger.warning("session_metadata delete failed for %s", session_id)

    def update_archived(
        self,
        session_id: str,
        archived_in: str | None,
        file_path: str | None = None,
    ) -> None:
        """Flip ``archived_in`` (and optionally the file path) for an
        existing row. Used by ``archive_old_sessions``."""
        try:
            if file_path is None:
                self._conn.execute(
                    "UPDATE session_metadata SET archived_in = ? "
                    "WHERE session_id = ?",
                    (archived_in, session_id),
                )
            else:
                self._conn.execute(
                    "UPDATE session_metadata "
                    "SET archived_in = ?, file_path = ? "
                    "WHERE session_id = ?",
                    (archived_in, file_path, session_id),
                )
            self._conn.commit()
        except Exception:
            logger.warning("session_metadata update_archived failed for %s", session_id)

    def rename(self, old_session_id: str, new_session_id: str, new_file_path: str) -> None:
        try:
            self._conn.execute(
                "UPDATE session_metadata "
                "SET session_id = ?, file_path = ?, date_prefix = ? "
                "WHERE session_id = ?",
                (
                    new_session_id,
                    new_file_path,
                    _date_prefix(new_session_id),
                    old_session_id,
                ),
            )
            self._conn.commit()
        except Exception:
            logger.warning(
                "session_metadata rename %s → %s failed",
                old_session_id, new_session_id,
            )

    # ── read paths ───────────────────────────────────────────────────

    def list_active_by_day(self) -> list[dict[str, Any]]:
        """Return the same shape as ``session_store.list_sessions_by_day``:
        active rows grouped by ``date_prefix``, newest-day-first, with
        ``custom`` last."""
        try:
            cursor = self._conn.execute(
                "SELECT session_id, started_at, ended_at, turn_count, model, "
                "date_prefix FROM session_metadata "
                "WHERE archived_in IS NULL "
                "ORDER BY date_prefix DESC, started_at DESC"
            )
        except sqlite3.OperationalError:
            return []
        rows = list(cursor)
        by_day: dict[str, list[dict[str, Any]]] = {}
        for sid, started_at, ended_at, turn_count, model, day in rows:
            by_day.setdefault(day, []).append({
                "session_id": sid,
                "started_at": started_at,
                "ended_at": ended_at,
                "turn_count": int(turn_count or 0),
                "model": model or "",
            })
        days = [
            {
                "date": day,
                "runs": runs,
                "run_count": len(runs),
                "total_turns": sum(r.get("turn_count", 0) for r in runs),
            }
            for day, runs in by_day.items()
        ]

        def _sort_key(d: dict[str, Any]) -> str:
            # Match `session_store.list_sessions_by_day::_sort_key`:
            # ISO dates with the "1" prefix beat the "custom" "0"
            # prefix under reverse=True, putting `custom` last.
            return "0" if d["date"] == "custom" else f"1{d['date']}"

        days.sort(key=_sort_key, reverse=True)
        return days

    def list_archive(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return archive rows newest-first up to ``limit``."""
        sql = (
            "SELECT session_id, started_at, ended_at, turn_count, model, "
            "archived_in FROM session_metadata "
            "WHERE archived_in IS NOT NULL "
            "ORDER BY started_at DESC"
        )
        params: tuple = ()
        if limit and limit > 0:
            sql += " LIMIT ?"
            params = (int(limit),)
        try:
            cursor = self._conn.execute(sql, params)
        except sqlite3.OperationalError:
            return []
        return [
            {
                "session_id": sid,
                "started_at": started_at,
                "ended_at": ended_at,
                "turn_count": int(turn_count or 0),
                "model": model or "",
                "archived_in": archived_in,
            }
            for sid, started_at, ended_at, turn_count, model, archived_in in cursor
        ]

    def count(self) -> int:
        try:
            cursor = self._conn.execute("SELECT COUNT(*) FROM session_metadata")
            return int(cursor.fetchone()[0])
        except Exception:
            return 0

    # ── maintenance ──────────────────────────────────────────────────

    def rebuild_from_disk(self, sessions_dir: Path) -> int:
        """Drop every row, re-walk ``sessions_dir`` + its ``archive/``
        subtree, and upsert one row per parseable JSON. Returns count.

        Idempotent: running again yields the same rows. Stale rows
        (file deleted, never indexed) are dropped because the rebuild
        starts from zero.
        """
        try:
            self._conn.execute("DELETE FROM session_metadata")
            self._conn.commit()
        except Exception:
            logger.exception("session_metadata rebuild: DELETE failed")
            return 0
        if not sessions_dir.exists():
            return 0
        n = 0
        for path in sorted(sessions_dir.glob("*.json")):
            if _ingest(self, path, archived_in=None):
                n += 1
        archive_root = sessions_dir / "archive"
        if archive_root.exists():
            for month_dir in archive_root.iterdir():
                if not month_dir.is_dir():
                    continue
                for path in sorted(month_dir.glob("*.json")):
                    if _ingest(self, path, archived_in=month_dir.name):
                        n += 1
        return n

    def prune_orphans(self) -> int:
        """Drop rows whose ``file_path`` no longer exists on disk.

        Lighter than ``rebuild_from_disk`` — touches only the rows
        that drifted. Used by the periodic maintenance hook so the
        index doesn't accumulate ghosts from deletions that bypassed
        the ``delete_session`` write-through.
        """
        try:
            cursor = self._conn.execute(
                "SELECT session_id, file_path FROM session_metadata"
            )
        except sqlite3.OperationalError:
            return 0
        dropped = 0
        for sid, path in list(cursor):
            try:
                if not Path(path).exists():
                    self._conn.execute(
                        "DELETE FROM session_metadata WHERE session_id = ?",
                        (sid,),
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


def _ingest(
    index: SessionMetadataIndex,
    path: Path,
    *,
    archived_in: str | None,
) -> bool:
    """Read one session JSON and upsert one row. Returns True on
    success, False on parse/read failure.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("session_metadata: could not read %s", path)
        return False
    index.upsert(SessionMetaRow(
        session_id=path.stem,
        started_at=str(data.get("started_at") or ""),
        ended_at=data.get("ended_at"),
        turn_count=int(data.get("turn_count") or 0),
        model=str(data.get("model") or ""),
        file_path=str(path),
        archived_in=archived_in,
    ))
    return True
