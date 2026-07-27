"""Per-chat persistence for mirror-multi-chat (P1).

Each chat in a ``ServerSession`` persists to
``<TESSERACT_HOME>/sessions/chats/<chat_id>.json``, independent of the legacy
per-session file (``sessions/<name>.json``). Files are canonical; the live
``ServerSession.chats`` registry is the in-memory source of truth for a
connected cockpit, flushed here on autosave and chat mutations (wired in a
later increment).

Format (schema 1)::

    {
      "schema": 1, "chat_id", "session_id", "title",
      "created_at", "started_at", "ended_at",
      "archived", "turn_count", "history": [...]
    }

chat_id is a uuid4 hex (32 lowercase hex chars) — validated on every path so a
crafted id can't escape the chats dir.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from tesseract.brain.session_store import (
    index_conversation_file,
    sanitize_history_for_persistence,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_CHAT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _is_valid_chat_id(chat_id: str) -> bool:
    return bool(_CHAT_ID_RE.fullmatch(chat_id or ""))


def chats_dir() -> Path:
    """Return ``<TESSERACT_HOME>/sessions/chats``, resolving env at call time.

    Matches the canonical env-or-default home pattern (env override wins so
    test fixtures using ``monkeypatch.setenv`` get an isolated dir).
    """
    override = os.environ.get("TESSERACT_HOME")
    if override:
        base = Path(override).resolve()
    else:
        from tesseract.paths import TESSERACT_HOME
        base = TESSERACT_HOME
    return base / "sessions" / "chats"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


@dataclass
class ChatRecord:
    chat_id: str
    session_id: str
    title: str
    created_at: str
    started_at: str
    history: list[dict[str, Any]] = field(default_factory=list)
    archived: bool = False
    turn_count: int = 0
    ended_at: str | None = None
    schema: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "archived": self.archived,
            "turn_count": self.turn_count,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatRecord:
        return cls(
            schema=data.get("schema", SCHEMA_VERSION),
            chat_id=data["chat_id"],
            session_id=data.get("session_id", ""),
            title=data.get("title", ""),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at"),
            archived=bool(data.get("archived", False)),
            turn_count=int(data.get("turn_count", 0)),
            history=data.get("history", []),
        )


def _chat_path(chat_id: str) -> Path:
    return chats_dir() / f"{chat_id}.json"


def save_chat(record: ChatRecord) -> Path:
    """Persist a chat to ``chats/<chat_id>.json``.

    Sanitizes attachment bytes out of history (raw files live under
    ``uploads/``), stamps ``ended_at``, and derives ``turn_count`` from the
    history so it can't drift from the saved messages. Raises ``ValueError``
    on a malformed chat_id.
    """
    if not _is_valid_chat_id(record.chat_id):
        raise ValueError(f"invalid chat_id: {record.chat_id!r}")
    directory = chats_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # Non-destructive: work on a copy so a caller that keeps using `record`
    # after the save doesn't find its history stripped / turn_count rewritten.
    record = replace(record)
    record.history = sanitize_history_for_persistence(record.history)
    record.turn_count = sum(1 for m in record.history if m.get("role") == "user")
    record.ended_at = _now_iso()
    path = _chat_path(record.chat_id)
    path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
    return path


def load_chat(chat_id: str) -> ChatRecord | None:
    """Load a chat, or None if missing / unreadable / invalid id."""
    if not _is_valid_chat_id(chat_id):
        return None
    path = _chat_path(chat_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        record = ChatRecord.from_dict(data)
    except Exception as exc:  # noqa: BLE001 — corrupt file shouldn't crash a list
        logger.warning("chat load failed (%s): %s", path, exc)
        return None
    record.history = sanitize_history_for_persistence(record.history)
    return record


def list_chats(*, include_archived: bool = False) -> list[dict[str, Any]]:
    """Return sidebar metadata rows (no history), newest-created first.

    Archived chats are excluded unless ``include_archived`` is set.
    """
    directory = chats_dir()
    if not directory.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        record = load_chat(path.stem)
        if record is None:
            continue
        if record.archived and not include_archived:
            continue
        rows.append({
            "chat_id": record.chat_id,
            "title": record.title,
            "created_at": record.created_at,
            "archived": record.archived,
            "message_count": len(record.history),
        })
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def set_archived(chat_id: str, archived: bool = True) -> bool:
    """Flip a chat's archived flag on disk. Returns False if it doesn't exist."""
    record = load_chat(chat_id)
    if record is None:
        return False
    record.archived = archived
    save_chat(record)
    return True


def rename_chat(chat_id: str, title: str) -> bool:
    """Set a chat's operator title. Returns False if it doesn't exist."""
    record = load_chat(chat_id)
    if record is None:
        return False
    record.title = title
    save_chat(record)
    return True


def persist_session_chats(session: Any) -> int:
    """Flush every chat in a live ``ServerSession`` to disk. Returns the count.

    Duck-typed (no import of ``ServerSession``) to keep this module free of a
    cycle: reads ``session.session_id`` / ``session.chats`` / ``session.chat_meta``.
    Persists open AND archived chats so archive state survives a restart. A
    single chat's failure is logged and skipped — one bad chat must not lose
    the others on session close.
    """
    saved = 0
    for chat_id, cs in dict(session.chats).items():
        meta = session.chat_meta.get(chat_id)
        if meta is None:
            continue
        try:
            save_chat(ChatRecord(
                chat_id=chat_id,
                session_id=session.session_id,
                title=meta.title,
                created_at=meta.created_at,
                started_at=meta.started_at,
                archived=meta.archived,
                history=list(getattr(cs, "history", []) or []),
            ))
            saved += 1
        except Exception:  # noqa: BLE001 — never lose other chats on one failure
            logger.exception("persist_session_chats: failed for chat %s", chat_id)
    return saved


def index_session_chats(session: Any) -> int:
    """Index every persisted chat into the CR-1 work index for recall.

    Each chat is indexed by its own ``sessions/chats/<chat_id>.json`` file, so
    ``recall_history`` surfaces background chats too — not just whichever chat
    was active at close (the legacy single-file save only captured that one).
    Best-effort and duck-typed (reads ``session.chats``); a single chat's
    failure is logged and skipped. Returns the count indexed.

    Call AFTER ``persist_session_chats`` so the files exist on disk. Chats with
    no history are skipped (an empty file yields no recall chunks).
    """
    indexed = 0
    for chat_id, cs in dict(session.chats).items():
        if not _is_valid_chat_id(chat_id):
            continue
        if not getattr(cs, "history", None):
            continue
        path = _chat_path(chat_id)
        if not path.exists():
            continue
        try:
            index_conversation_file(path)
            indexed += 1
        except Exception:  # noqa: BLE001 — never block close on one chat's indexer
            logger.exception("index_session_chats: failed for chat %s", chat_id)
    return indexed


def _last_message_timestamp(history: list[dict[str, Any]]) -> str | None:
    """Timestamp of the most recent message actually appended to this chat.

    Walks ``history`` in reverse for the first entry carrying a ``timestamp``
    field (stamped per-message in ``brain/chat.py``, survives persistence —
    ``sanitize_history_for_persistence`` only strips attachment bytes).
    Older/loaded entries without one are skipped. Returns None if nothing
    in the history is stamped (e.g. an empty chat, or history predating the
    per-message timestamp field).
    """
    for msg in reversed(history):
        ts = msg.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return None


def _last_activity_date(record: ChatRecord) -> str | None:
    """Local calendar date (``YYYY-MM-DD``) this chat was last actually used.

    Prefers the last message's own timestamp over the record-level
    ``ended_at``/``started_at``/``created_at``: ``persist_session_chats``
    re-stamps ``ended_at`` for EVERY open chat in a session on any single
    chat's persist (create/rename/archive/autosave), not just the chat that
    changed — so ``ended_at`` alone can make a chat the operator hasn't
    touched in days look "active today" the moment a sibling chat is saved.
    A message timestamp is scoped to the chat it lives in and can't be
    laundered that way. Falls back to the record-level fields only for a
    chat with no stamped messages (e.g. a freshly-created empty chat).
    Message timestamps may be UTC while record fields are local-zone;
    ``.astimezone()`` normalizes either to the machine's local calendar date.
    Returns None when nothing parses — callers treat that as "don't know,
    leave it alone" rather than guessing.
    """
    stamp = (
        _last_message_timestamp(record.history)
        or record.ended_at
        or record.started_at
        or record.created_at
    )
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).astimezone().date().isoformat()
    except ValueError:
        return None


def archive_stale_open_chats(today: str | None = None) -> int:
    """Day-rollover — auto-archive open chats last touched before today.

    Operator request (2026-07-05): a fresh Mirror connection on a new local
    calendar day should seed a blank chat rather than resume yesterday's
    thread, while cost/turns (already day-scoped elsewhere) reset in step.
    Called by ``session.py::_restore_persisted_chats`` before it rebuilds the
    tab strip from disk — archiving (not deleting) means the stale chat stays
    fully reachable via ``GET /api/chats?include_archived=1`` and the
    ``chat.restore`` WS command, same as any operator-archived chat. A record
    with no parseable timestamp is left open (fail-safe, not fail-archive).
    Returns the count archived.

    Known limitation: this reads/writes the global on-disk chat library with
    no cross-connection lock. Two WS connections spanning the same midnight
    rollover (e.g. one tab left open overnight, a second opened the next
    morning) could interleave — the second tab's archive here racing the
    first tab's still-live in-memory ``chat_meta`` for the same chat. Rare
    (needs two concurrent connections straddling local midnight) and left
    unhandled; a session/tab that hits it can always re-fetch via
    ``GET /api/chats``.
    """
    today = today or datetime.now().astimezone().date().isoformat()
    archived = 0
    for row in list_chats():
        record = load_chat(row["chat_id"])
        if record is None:
            continue
        last_active = _last_activity_date(record)
        if last_active is not None and last_active != today:
            if set_archived(record.chat_id, True):
                archived += 1
    return archived


def delete_chat(chat_id: str) -> tuple[bool, str]:
    """Hard-delete a chat file.

    Returns ``(ok, reason)``: ``(True, "")`` deleted; ``(False, "invalid_id")``
    malformed id; ``(False, "not_found")`` no such file; ``(False, "io_error")``
    unlink failed. Archive-before-delete policy (D1) is enforced by the route
    layer, not here.
    """
    if not _is_valid_chat_id(chat_id):
        return False, "invalid_id"
    path = _chat_path(chat_id)
    if not path.exists():
        return False, "not_found"
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("chat delete failed (%s): %s", path, exc)
        return False, "io_error"
    return True, ""
