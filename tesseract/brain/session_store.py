"""Session persistence — save and load ChatSession history across REPL runs.

JSON on disk at `tesseract/sessions/*.json`. Each file holds the message
history + metadata. Reloading replays the messages into a ChatSession so
the next turn has context.

Format (forward-compatible):
{
  "schema": 1,
  "started_at": "2026-04-16T22:34:00Z",
  "ended_at":   "2026-04-17T00:12:00Z",
  "turn_count": 42,
  "model":      "gpt-5-mini",
  "history":    [ ... message list ... ]
}
"""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)


_SOUL_INCREMENT_WARNED: bool = False


def _ruamel_rt() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    # Emit `null` (not bare empty) for None values, matching SOUL.md convention.
    y.representer.add_representer(
        type(None),
        lambda dumper, _: dumper.represent_scalar("tag:yaml.org,2002:null", "null"),
    )
    return y


def _increment_soul_interaction_count() -> None:
    """Increment SOUL.md::interaction_count by one, atomically.

    Preserves frontmatter key order, scalar styles (null vs ~), and comments
    via ruamel round-trip. Atomic write via os.replace. Fail-open: any error
    is logged at most once per process — never blocks session save.
    """
    global _SOUL_INCREMENT_WARNED
    from tesseract.paths import workspace_dir
    soul = workspace_dir() / "SOUL.md"
    if not soul.exists():
        return
    raw = soul.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        return
    # Split: ['', frontmatter, body]
    parts = raw.split("---\n", 2)
    if len(parts) != 3:
        return
    _, front_text, body = parts

    yaml_rt = _ruamel_rt()
    try:
        doc = yaml_rt.load(front_text)
    except Exception:
        if not _SOUL_INCREMENT_WARNED:
            logger.exception("save_session: SOUL.md frontmatter parse failed")
            _SOUL_INCREMENT_WARNED = True
        return
    if doc is None:
        return

    current = doc.get("interaction_count") or 0
    try:
        doc["interaction_count"] = int(current) + 1
    except (TypeError, ValueError):
        if not _SOUL_INCREMENT_WARNED:
            logger.exception("save_session: SOUL.md interaction_count not numeric")
            _SOUL_INCREMENT_WARNED = True
        return

    buf = io.StringIO()
    yaml_rt.dump(doc, buf)
    new_front_text = buf.getvalue()
    new_content = f"---\n{new_front_text}---\n{body}"

    # Atomic write: tmpfile + os.replace
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".soul-", suffix=".md.tmp", dir=str(soul.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.replace(tmp_path, soul)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


SCHEMA_VERSION = 1

# OpenAI's encrypted_content blobs have an opaque TTL (~30 days observed, not
# guaranteed). Strip them from loaded sessions older than this to avoid 400s
# from stale reasoning items. Text history is preserved — only reasoning blobs
# (marked `_reasoning: True`) are dropped. Operator chose 7 days
# (2026-05-14) — well inside the observed TTL window and a tighter
# session-isolation guarantee than the original 14d.
REASONING_BLOB_MAX_AGE_DAYS = 7


@dataclass
class SessionState:
    started_at: str
    ended_at: str | None
    turn_count: int
    model: str
    history: list[dict[str, Any]] = field(default_factory=list)
    schema: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turn_count": self.turn_count,
            "model": self.model,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        return cls(
            schema=data.get("schema", SCHEMA_VERSION),
            started_at=data["started_at"],
            ended_at=data.get("ended_at"),
            turn_count=data.get("turn_count", 0),
            model=data.get("model", ""),
            history=data.get("history", []),
        )


def _now_iso() -> str:
    # Session JSON on disk is a human-read surface — write local-zone ISO
    # with offset. `_strip_stale_reasoning` parses back via `fromisoformat`
    # which preserves tzinfo, so age comparisons stay correct.
    return datetime.now().astimezone().isoformat()


def save_session(
    session_dir: Path,
    name: str,
    model: str,
    started_at: str,
    history: list[dict[str, Any]],
    *,
    index_work: bool = True,
) -> Path:
    path = session_file(session_dir, name)
    if path is None:
        raise ValueError(f"invalid session name: {name!r}")
    session_dir.mkdir(parents=True, exist_ok=True)
    state = SessionState(
        started_at=started_at,
        ended_at=_now_iso(),
        turn_count=sum(1 for m in history if m.get("role") == "user"),
        model=model,
        history=sanitize_history_for_persistence(history),
    )
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    try:
        _increment_soul_interaction_count()
    except Exception:  # never block session-save on workspace IO
        global _SOUL_INCREMENT_WARNED
        if not _SOUL_INCREMENT_WARNED:
            logger.exception("save_session: failed to increment interaction_count")
            _SOUL_INCREMENT_WARNED = True
    # CR-1 follow-up (M2): index the freshly-saved session into the
    # work-history index so `recall_history` and the future retrieval
    # merge can surface it on the next turn — covers REPL exit + Mirror
    # close + manual /save paths with one hook. Best-effort:
    # `index_conversation_file` swallows its own exceptions so a failed
    # indexer never blocks the save.
    # Mirror's multi-chat autosave passes index_work=False because it indexes
    # each chat's own `sessions/chats/<chat_id>.json` separately — indexing the
    # legacy active-chat snapshot too would double-index that chat's recall.
    # REPL / operator `/save` keep the default (no per-chat store there).
    if index_work:
        try:
            index_conversation_file(path)
        except Exception:  # noqa: BLE001 — never block save on indexer faults
            logger.exception("save_session: work-index hook raised (swallowed)")
    # 2026-05-23: write-through to the session-metadata derived index
    # so the Mirror drawer's list path can read from SQLite instead of
    # walking + JSON-parsing every file on every render.
    try:
        _maybe_upsert_session_metadata(path, state, archived_in=None)
    except Exception:  # noqa: BLE001 — never block save on metadata hook
        logger.exception("save_session: session_metadata hook raised (swallowed)")
    return path


def _session_metadata_index_path() -> Path:
    """Resolve the session-metadata sqlite path via the canonical
    env-or-default home pattern. Same shape as ``index_conversation_file``.
    """
    from tesseract.paths import TESSERACT_HOME as _DEFAULT_HOME

    home = Path(os.environ.get("TESSERACT_HOME") or _DEFAULT_HOME)
    return home / "session_metadata.sqlite"


def _maybe_upsert_session_metadata(
    path: Path,
    state: "SessionState",
    *,
    archived_in: str | None,
) -> None:
    """Mirror a save into the session-metadata derived index. Best-effort."""
    try:
        from tesseract.memory.session_metadata import (
            SessionMetadataIndex,
            SessionMetaRow,
        )
    except Exception:  # noqa: BLE001
        return
    try:
        idx = SessionMetadataIndex(_session_metadata_index_path())
    except Exception:  # noqa: BLE001
        return
    try:
        idx.upsert(SessionMetaRow(
            session_id=path.stem,
            started_at=state.started_at,
            ended_at=state.ended_at,
            turn_count=int(state.turn_count or 0),
            model=state.model or "",
            file_path=str(path),
            archived_in=archived_in,
        ))
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            idx.close()
        except Exception:  # noqa: BLE001
            pass


def _maybe_delete_session_metadata(session_id: str) -> None:
    try:
        from tesseract.memory.session_metadata import SessionMetadataIndex
    except Exception:  # noqa: BLE001
        return
    try:
        idx = SessionMetadataIndex(_session_metadata_index_path())
    except Exception:  # noqa: BLE001
        return
    try:
        idx.delete(session_id)
    finally:
        try:
            idx.close()
        except Exception:  # noqa: BLE001
            pass


def _maybe_rename_session_metadata(
    old_id: str, new_id: str, new_path: Path,
) -> None:
    try:
        from tesseract.memory.session_metadata import SessionMetadataIndex
    except Exception:  # noqa: BLE001
        return
    try:
        idx = SessionMetadataIndex(_session_metadata_index_path())
    except Exception:  # noqa: BLE001
        return
    try:
        idx.rename(old_id, new_id, str(new_path))
    finally:
        try:
            idx.close()
        except Exception:  # noqa: BLE001
            pass


def _maybe_archive_session_metadata(
    session_id: str, archived_in: str, new_path: Path,
) -> None:
    try:
        from tesseract.memory.session_metadata import SessionMetadataIndex
    except Exception:  # noqa: BLE001
        return
    try:
        idx = SessionMetadataIndex(_session_metadata_index_path())
    except Exception:  # noqa: BLE001
        return
    try:
        idx.update_archived(session_id, archived_in, str(new_path))
    finally:
        try:
            idx.close()
        except Exception:  # noqa: BLE001
            pass


def _list_active_from_index() -> list[dict[str, Any]]:
    """Read the active-sessions view from the metadata index. Returns
    `[]` on miss / empty / failure so the caller falls back to disk.
    """
    try:
        from tesseract.memory.session_metadata import SessionMetadataIndex
    except Exception:  # noqa: BLE001
        return []
    try:
        idx = SessionMetadataIndex(_session_metadata_index_path())
    except Exception:  # noqa: BLE001
        return []
    try:
        return idx.list_active_by_day()
    finally:
        try:
            idx.close()
        except Exception:  # noqa: BLE001
            pass


def _list_archive_from_index(limit: int) -> list[dict[str, Any]]:
    try:
        from tesseract.memory.session_metadata import SessionMetadataIndex
    except Exception:  # noqa: BLE001
        return []
    try:
        idx = SessionMetadataIndex(_session_metadata_index_path())
    except Exception:  # noqa: BLE001
        return []
    try:
        return idx.list_archive(limit=limit)
    finally:
        try:
            idx.close()
        except Exception:  # noqa: BLE001
            pass


def index_conversation_file(path: Path) -> None:
    """Index a freshly-saved conversation JSON into the CR-1 work index.

    Works for both legacy per-session files (``sessions/<name>.json``) and
    per-chat files (``sessions/chats/<chat_id>.json``) — both carry a
    ``history`` array, and ``index_session_file`` keys recall chunks by the
    file stem (the session name or chat_id), so each conversation is a
    separately-tagged recall source.

    Best-effort: any failure is swallowed (the save already succeeded).
    Resolves the DB path via the canonical ``env-or-import-constant``
    pattern — env override wins (test fixtures using ``monkeypatch.setenv``
    get isolated indexes), default falls back to
    ``tesseract.paths.TESSERACT_HOME`` so dev / default-home runtimes
    index instead of silently skipping. Matches `tesseract/kernel/
    workspace_changes.py::workspace_events_dir`.
    """
    try:
        from tesseract.memory.work_index import WorkIndex
        from tesseract.memory.work_ingester import index_session_file
        from tesseract.paths import TESSERACT_HOME as _DEFAULT_HOME
    except Exception:  # noqa: BLE001
        return
    home = Path(os.environ.get("TESSERACT_HOME") or _DEFAULT_HOME)
    db_path = home / "work_index.sqlite"
    try:
        idx = WorkIndex(db_path)
    except Exception:  # noqa: BLE001
        return
    try:
        index_session_file(idx, path)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            idx.close()
        except Exception:  # noqa: BLE001
            pass


def sanitize_history_for_persistence(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a save-safe history copy.

    Chat turns may temporarily carry base64 attachment bytes while an adapter
    request is in flight. Session JSON keeps metadata only; raw files already
    live under TESSERACT_HOME/uploads/chat.
    """
    sanitized = copy.deepcopy(history)
    for msg in sanitized:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        cleaned: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            cleaned.append({k: v for k, v in part.items() if k != "data"})
        msg["content"] = cleaned
    return sanitized


def load_session(
    path: Path,
    *,
    strip_reasoning: bool = False,
) -> SessionState | None:
    """Load a saved session from disk.

    `strip_reasoning` controls the TTL-based reasoning-blob purge. Default
    is False so listing/preview routes stay cheap. Resume callers (chat
    /load, /compact_file, GET /api/sessions/{id} when used to rehydrate
    the active chat) must pass True so an expired blob never reaches the
    API.
    """
    if not path.exists():
        logger.warning("session file missing: %s", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = SessionState.from_dict(data)
    except Exception as e:
        logger.warning("session load failed (%s): %s", path, e)
        return None
    state.history = sanitize_history_for_persistence(state.history)
    if strip_reasoning:
        _strip_stale_reasoning(state)
    return state


def _strip_stale_reasoning(state: SessionState) -> None:
    """Drop `_reasoning` items if the session is older than the blob TTL.

    Edits `state.history` in place. No-op when the session is recent or has
    no reasoning items.
    """
    stamp = state.ended_at or state.started_at
    if not stamp:
        return
    try:
        saved_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return
    age = datetime.now(timezone.utc) - saved_at
    if age < timedelta(days=REASONING_BLOB_MAX_AGE_DAYS):
        return
    before = len(state.history)
    state.history = [m for m in state.history if not m.get("_reasoning")]
    dropped = before - len(state.history)
    if dropped:
        logger.info(
            "stripped %d stale reasoning item(s) from session (age=%dd)",
            dropped, age.days,
        )


def list_sessions(session_dir: Path, limit: int = 10) -> list[tuple[Path, SessionState]]:
    """Return recent ACTIVE sessions sorted newest-first by ended_at.

    Excludes anything under `<session_dir>/archive/` — the archive holds
    per-run files older than `ARCHIVE_AGE_DAYS` moved by the scheduler
    `sessions_archive` task. `glob("*.json")` is non-recursive so the
    archive folder is excluded naturally.
    """
    if not session_dir.exists():
        return []
    entries: list[tuple[Path, SessionState]] = []
    for p in session_dir.glob("*.json"):
        s = load_session(p)
        if s is not None:
            entries.append((p, s))
    entries.sort(key=lambda t: t[1].ended_at or t[1].started_at, reverse=True)
    return entries[:limit]


# Phase 1 (CLI parity) — auto-archive threshold in days. Per-run files
# whose date prefix is older than this land in
# `<session_dir>/archive/YYYY-MM/` on the daily `sessions_archive`
# scheduler tick. Operator chose 7 (2026-05-10) — keeps the live
# drawer one calendar week of context, archive holds everything else.
ARCHIVE_AGE_DAYS = 7

# Per-run filename pattern: YYYY-MM-DD-HHMM.json, e.g. 2026-05-10-2025.json.
# Used to extract the date prefix for grouping + age comparison.
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-\d{4})?$")


def _extract_session_date(stem: str) -> str | None:
    """Return the YYYY-MM-DD prefix from a session filename stem, or None
    when the name doesn't match the canonical pattern (custom names like
    `before-rebase` — operator-chosen, treated as undated)."""
    m = _DATE_PREFIX_RE.match(stem)
    return m.group(1) if m else None


def list_sessions_by_day(session_dir: Path) -> list[dict[str, Any]]:
    """Group active per-run sessions by their YYYY-MM-DD prefix.

    Returns a list of `{date, runs: [{name, started_at, ended_at,
    turn_count, model}], total_turns}` newest-day-first. Custom-named
    sessions (no date prefix) land in a synthetic `"custom"` bucket
    sorted last so the date-ordered runs stay coherent.

    Mirror drawer consumes this via `GET /api/sessions/days`.

    Reads from the SQLite metadata index when populated; falls back to
    the disk walk when the index is empty (e.g. first run before
    backfill, or test fixtures that bypass save_session). Files stay
    canonical — the index is rebuildable.
    """
    fast = _list_active_from_index()
    if fast:
        return fast
    if not session_dir.exists():
        return []
    by_day: dict[str, list[dict[str, Any]]] = {}
    for p in session_dir.glob("*.json"):
        state = load_session(p)
        if state is None:
            continue
        stem = p.stem
        day = _extract_session_date(stem) or "custom"
        by_day.setdefault(day, []).append({
            "session_id": stem,
            "started_at": state.started_at,
            "ended_at": state.ended_at,
            "turn_count": state.turn_count,
            "model": state.model,
        })

    days: list[dict[str, Any]] = []
    for day_key, runs in by_day.items():
        # Sort runs within a day by started_at descending — most recent
        # run of the day at top.
        runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        days.append({
            "date": day_key,
            "runs": runs,
            "run_count": len(runs),
            "total_turns": sum(r.get("turn_count", 0) for r in runs),
        })

    def _sort_key(d: dict[str, Any]) -> str:
        # Real ISO dates have year 4 digits in the 2000-2099 range for
        # any realistic session, so the "1" prefix puts them all in a
        # contiguous bucket above "custom" (prefix "0") under
        # reverse=True. If anyone ever stores a session with a 5-digit
        # year stem the comparison still produces a stable order, just
        # not the strictly-newest-first one — acceptable for that
        # hypothetical edge case.
        return "0" if d["date"] == "custom" else f"1{d['date']}"

    days.sort(key=_sort_key, reverse=True)
    return days


ARCHIVE_LIST_DEFAULT_LIMIT = 200


def list_archive(
    session_dir: Path,
    *,
    limit: int = ARCHIVE_LIST_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """List archived per-run files under `<session_dir>/archive/YYYY-MM/`.

    Returns `{session_id, archived_in: 'YYYY-MM', started_at,
    ended_at, turn_count, model}` rows newest-first, capped at
    `limit`. The archive grows monotonically over years; the cap
    keeps the route handler bounded under unrealistic backlogs.
    Operator with a deeper archive can raise the cap or load the
    files directly from disk.

    Reads from the SQLite metadata index when populated; falls back to
    the disk walk when the index is empty.
    """
    fast = _list_archive_from_index(limit)
    if fast:
        return fast
    archive_root = session_dir / "archive"
    if not archive_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for month_dir in archive_root.iterdir():
        if not month_dir.is_dir():
            continue
        month_key = month_dir.name
        for p in month_dir.glob("*.json"):
            state = load_session(p)
            if state is None:
                continue
            rows.append({
                "session_id": p.stem,
                "archived_in": month_key,
                "started_at": state.started_at,
                "ended_at": state.ended_at,
                "turn_count": state.turn_count,
                "model": state.model,
            })
    rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return rows[:limit] if limit > 0 else rows


def archive_old_sessions(
    session_dir: Path,
    *,
    days: int = ARCHIVE_AGE_DAYS,
    now: datetime | None = None,
) -> list[Path]:
    """Move per-run session files older than `days` into
    `<session_dir>/archive/YYYY-MM/` based on their date prefix.

    Returns the list of destination paths actually moved (post-move).
    Files without a parseable date prefix (custom-named) are skipped.
    Idempotent: running again moves nothing more.

    The scheduler `sessions_archive` task calls this once per day.
    """
    if not session_dir.exists():
        return []
    cutoff_dt = (now or datetime.now(timezone.utc)).date() - timedelta(days=days)
    moved: list[Path] = []
    archive_root = session_dir / "archive"
    for p in session_dir.glob("*.json"):
        date_str = _extract_session_date(p.stem)
        if not date_str:
            continue  # custom names stay where the operator put them
        try:
            file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date >= cutoff_dt:
            continue
        month_dir = archive_root / file_date.strftime("%Y-%m")
        try:
            month_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("sessions_archive mkdir failed (%s): %s", month_dir, exc)
            continue
        dst = month_dir / p.name
        if dst.exists():
            # Concurrent run or replay — leave both files alone; operator
            # can manually reconcile.
            logger.warning(
                "sessions_archive skipped %s — destination already exists at %s",
                p, dst,
            )
            continue
        try:
            p.rename(dst)
        except OSError as exc:
            logger.warning("sessions_archive rename failed (%s -> %s): %s", p, dst, exc)
            continue
        moved.append(dst)
        # Sync the archival decision into the metadata index. The row
        # already exists (created on the original save); we just flip
        # `archived_in` and update the path.
        try:
            _maybe_archive_session_metadata(
                p.stem, file_date.strftime("%Y-%m"), dst,
            )
        except Exception:  # noqa: BLE001
            logger.exception("archive_old_sessions: metadata hook raised (swallowed)")
    if moved:
        logger.info("sessions_archive moved %d file(s) into archive/", len(moved))
    return moved


def default_session_name() -> str:
    """Timestamp-based name: 2026-04-16-2234.json"""
    return datetime.now().strftime("%Y-%m-%d-%H%M")


def delete_session(session_dir: Path, name: str) -> tuple[bool, str]:
    """Delete a saved session file.

    Returns (ok, reason):
      (True,  "")             — deleted
      (False, "invalid_name") — the name could name a file outside session_dir
      (False, "not_found")    — no file at that path (operator typo or already gone)
      (False, "io_error")     — unlink raised OSError
    The reason discriminates so the Mirror can surface a warning toast for
    not_found (operator-recoverable) vs an error toast for io_error.
    """
    path = session_file(session_dir, name)
    if path is None:
        return False, "invalid_name"
    if not path.exists():
        return False, "not_found"
    try:
        path.unlink()
    except OSError as e:
        logger.warning("session delete failed (%s): %s", path, e)
        return False, "io_error"
    # Keep the derived metadata index in sync. Best-effort; the file is
    # already gone, the row is just bookkeeping.
    try:
        _maybe_delete_session_metadata(name)
    except Exception:  # noqa: BLE001
        logger.exception("delete_session: metadata hook raised (swallowed)")
    return True, ""


# Path slug rules — keep filenames operator-readable and OS-safe. Allowed:
# ASCII letters, digits, dot, dash, underscore. Disallowed everywhere else:
# any slash, backslash, colon, leading/trailing whitespace, leading dots,
# NULs, and non-ASCII characters (so cross-platform repo sync — Phase 17 —
# can't hit NFC/NFD filename collisions on macOS). Empty slugs rejected.
_SLUG_MAX_LEN = 80


def _is_valid_slug(slug: str) -> bool:
    if not slug or len(slug) > _SLUG_MAX_LEN:
        return False
    if slug.startswith(".") or slug != slug.strip():
        return False
    return all((c.isascii() and c.isalnum()) or c in "._-" for c in slug)


def session_file(session_dir: Path, name: str) -> Path | None:
    """Resolve a saved session's file, or None if `name` could name another one.

    The `.json` suffix is optional, matching what the callers accepted before
    this existed. `rename`, `duplicate` and `preview` have validated the same
    string all along; `GET /api/sessions/{session_id}` and `/load` joined it
    raw, and `{session_id}` excludes `/` and nothing else — so on the platform
    this ships on `..\\` climbed out and `C:x` discarded the directory entirely.
    """
    if not _is_valid_slug(name.removesuffix(".json")):
        return None
    return session_dir / f"{name.removesuffix('.json')}.json"


def rename_session(session_dir: Path, old_name: str, new_name: str) -> tuple[bool, str]:
    """Rename a saved session file in place.

    Returns (ok, reason):
      (True,  "")            — renamed; new path is `session_dir / new_name.json`
      (False, "not_found")   — `old_name.json` does not exist
      (False, "invalid_name")— `new_name` fails slug validation (path traversal,
                                empty, too long, leading dot, illegal chars)
      (False, "exists")      — `new_name.json` already exists; refuse to overwrite
      (False, "io_error")    — rename raised OSError
    """
    new_name = (new_name or "").removesuffix(".json")
    old_name = (old_name or "").removesuffix(".json")
    if not _is_valid_slug(new_name) or not _is_valid_slug(old_name):
        return False, "invalid_name"
    src = session_dir / f"{old_name}.json"
    dst = session_dir / f"{new_name}.json"
    if not src.exists():
        return False, "not_found"
    if src == dst:
        return True, ""
    if dst.exists():
        return False, "exists"
    try:
        src.rename(dst)
    except OSError as e:
        logger.warning("session rename failed (%s -> %s): %s", src, dst, e)
        return False, "io_error"
    try:
        _maybe_rename_session_metadata(old_name, new_name, dst)
    except Exception:  # noqa: BLE001
        logger.exception("rename_session: metadata hook raised (swallowed)")
    return True, ""


def duplicate_session(
    session_dir: Path, source_name: str, dest_name: str
) -> tuple[bool, str]:
    """Copy a saved session to a new name.

    Returns (ok, reason) — same shape as rename_session.
    """
    source_name = (source_name or "").removesuffix(".json")
    dest_name = (dest_name or "").removesuffix(".json")
    if not _is_valid_slug(source_name) or not _is_valid_slug(dest_name):
        return False, "invalid_name"
    src = session_dir / f"{source_name}.json"
    dst = session_dir / f"{dest_name}.json"
    if not src.exists():
        return False, "not_found"
    if dst.exists():
        return False, "exists"
    try:
        dst.write_bytes(src.read_bytes())
    except OSError as e:
        logger.warning("session duplicate failed (%s -> %s): %s", src, dst, e)
        return False, "io_error"
    # Mirror the duplicate into the metadata index by reloading and
    # upserting. Cheap (one JSON parse), keeps the drawer in sync.
    try:
        state = load_session(dst)
        if state is not None:
            _maybe_upsert_session_metadata(dst, state, archived_in=None)
    except Exception:  # noqa: BLE001
        logger.exception("duplicate_session: metadata hook raised (swallowed)")
    return True, ""


def preview_session(
    session_dir: Path, name: str, max_turns: int = 6
) -> dict[str, Any] | None:
    """Return a lightweight preview of the saved session — first N user/
    assistant turns with their text, no tool calls, no reasoning blobs.
    Returns None when the session can't be loaded.
    """
    name = (name or "").removesuffix(".json")
    if not _is_valid_slug(name):
        return None
    state = load_session(session_dir / f"{name}.json")
    if state is None:
        return None
    turns: list[dict[str, Any]] = []
    for msg in state.history:
        if len(turns) >= max_turns:
            break
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        if msg.get("_reasoning"):
            continue
        content = msg.get("content")
        text = _extract_text(content)
        if not text:
            continue
        turns.append({"role": role, "text": text[:600]})
    return {
        "session_id": name,
        "started_at": state.started_at,
        "ended_at": state.ended_at,
        "turn_count": state.turn_count,
        "model": state.model,
        "turns": turns,
    }


def _extract_text(content: Any) -> str:
    """OpenAI/Gemini histories use mixed content shapes — stringify just
    enough to render a preview. Tool-call blocks and image parts are
    skipped to keep the popover readable."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in ("text", "input_text", "output_text"):
                    val = item.get("text") or item.get("content") or ""
                    if isinstance(val, str):
                        parts.append(val)
        return " ".join(parts).strip()
    return ""
