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
      "archived", "turn_count", "model", "history": [...]
    }

chat_id is a uuid4 hex (32 lowercase hex chars) — validated on every path so a
crafted id can't escape the chats dir.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from tesseract.brain.session_store import (
    extract_message_text,
    index_conversation_file,
    sanitize_history_for_persistence,
)
from tesseract.lib.yaml_io import atomic_write_text

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_CHAT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_T = TypeVar("_T")

#: An index connection held open across a burst of writes — see ``_index_batch``.
_batch = threading.local()


def _is_valid_chat_id(chat_id: str) -> bool:
    return bool(_CHAT_ID_RE.fullmatch(chat_id or ""))


def chats_dir() -> Path:
    """Return ``<TESSERACT_HOME>/sessions/chats``, resolving env at call time.

    Matches the canonical env-or-default home pattern (env override wins so
    test fixtures using ``monkeypatch.setenv`` get an isolated dir).
    """
    return _home() / "sessions" / "chats"


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
    model: str = ""
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
            "model": self.model,
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
            model=str(data.get("model") or ""),
            history=data.get("history", []),
        )


def _chat_path(chat_id: str) -> Path:
    return chats_dir() / f"{chat_id}.json"


def _home() -> Path:
    """``TESSERACT_HOME``, resolved at call time.

    Env override wins so a test fixture's ``monkeypatch.setenv`` reaches the
    derived stores this module writes to, not the operator's.
    """
    override = os.environ.get("TESSERACT_HOME")
    if override:
        return Path(override).resolve()
    from tesseract.paths import TESSERACT_HOME

    return TESSERACT_HOME


def metadata_index_path() -> Path:
    """``<TESSERACT_HOME>/chat_metadata.sqlite``, resolved at call time.

    Same env-or-default home pattern as ``chats_dir`` — a test fixture setting
    ``TESSERACT_HOME`` gets an isolated index rather than the operator's.
    """
    return _home() / "chat_metadata.sqlite"


def _with_index(action: Callable[[Any], _T], default: _T) -> _T:
    """Run one action against the derived index. Best-effort, always closed.

    The index is derived and rebuildable, so a failure here must never cost a
    write to the canonical record — every caller passes what it wants back
    when the index is unreachable.

    Inside an ``_index_batch`` the held connection is reused instead of opened.
    """
    held = getattr(_batch, "index", None)
    if held is not None:
        try:
            return action(held)
        except Exception:  # noqa: BLE001
            logger.warning("chat_metadata: index action failed", exc_info=True)
            return default
    try:
        from tesseract.memory.chat_metadata import ChatMetadataIndex

        index = ChatMetadataIndex(metadata_index_path())
    except Exception:  # noqa: BLE001
        return default
    try:
        return action(index)
    except Exception:  # noqa: BLE001
        logger.warning("chat_metadata: index action failed", exc_info=True)
        return default
    finally:
        try:
            index.close()
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def _index_batch():
    """Hold one index connection open across a burst of writes.

    Opening one costs ~8 ms — the WAL pragma and the schema check, not the
    query — and ``persist_session_chats`` saves every chat in a session on a
    timer, ON THE EVENT LOOP. Per-chat that is 8 ms times however many
    conversations the operator has open, which crosses the 50 ms bar that
    keeps health checks and inbound turns responsive; per burst it is 8 ms
    once. Thread-local because a sqlite connection belongs to the thread that
    opened it.

    Re-entering reuses the connection already held rather than opening a second
    one — a nested batch replacing it would leak the outer connection and end
    its transaction early.
    """
    if getattr(_batch, "index", None) is not None:
        yield
        return
    try:
        from tesseract.memory.chat_metadata import ChatMetadataIndex

        _batch.index = ChatMetadataIndex(metadata_index_path())
    except Exception:  # noqa: BLE001
        _batch.index = None
    index = getattr(_batch, "index", None)
    if index is None:
        try:
            yield
        finally:
            _batch.index = None
        return
    try:
        with index.deferred():
            yield
    finally:
        _batch.index = None
        try:
            index.close()
        except Exception:  # noqa: BLE001
            pass


def _meta_row(record: ChatRecord, path: Path) -> Any:
    from tesseract.memory.chat_metadata import ChatMetaRow

    return ChatMetaRow(
        chat_id=record.chat_id,
        title=record.title,
        created_at=record.created_at,
        started_at=record.started_at,
        ended_at=record.ended_at,
        turn_count=record.turn_count,
        model=record.model,
        archived=record.archived,
        file_path=str(path),
    )


def rebuild_metadata_index() -> int:
    """Rebuild the derived index from the records on disk. Returns the count.

    The walk is this module's, not the index's — one owner of the directory,
    which is what ``GOVERNANCE.md`` §3 asks for and what the retiring index
    broke by globbing ``sessions/`` itself.
    """
    rows = []
    for path in iter_history_files():
        record = load_chat(path.stem)
        if record is None:
            continue
        rows.append(_meta_row(record, path))
    return _with_index(lambda index: index.replace_all(rows), 0)


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
    # Atomic, not `write_text`: autosave rewrites this file on a timer, so a
    # kill or power cut during a write is the exact event it exists to survive
    # — and a truncated file is worse than a stale one, because `load_chat`
    # discards malformed JSON and the chat is then simply gone. Temp-then-
    # replace keeps the previous good copy until the new one is complete.
    atomic_write_text(path, json.dumps(record.to_dict(), indent=2))
    # Write-through to the derived index, so the day view stays current between
    # rebuilds. Every mutation the runtime makes to a record — autosave, rename,
    # archive, restore — lands here, which is why none of them needs its own hook.
    _with_index(lambda index: index.upsert(_meta_row(record, path)), None)
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


def list_chats(
    *, include_archived: bool = False, archived_only: bool = False
) -> list[dict[str, Any]]:
    """Return sidebar metadata rows (no history), newest-created first.

    Three answers, because the drawer asks three questions: open chats
    (default), open AND archived (``include_archived``), and the archive
    section's archived-only (``archived_only``, which wins). ``include_archived``
    is a widener rather than a filter, which is why the third one had to exist —
    the archive list would otherwise render every open chat as archived.
    """
    rows: list[dict[str, Any]] = []
    for path in iter_history_files():
        record = load_chat(path.stem)
        if record is None:
            continue
        if not _wanted(record.archived, include_archived, archived_only):
            continue
        rows.append({
            "chat_id": record.chat_id,
            "title": record.title,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "turn_count": record.turn_count,
            "model": record.model,
            "archived": record.archived,
            "message_count": len(record.history),
        })
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def _wanted(archived: bool, include_archived: bool, archived_only: bool) -> bool:
    """The three questions the drawer asks, answered in one place.

    Open chats (default), open AND archived (``include_archived``), or the
    archive section's archived-only (``archived_only``, which wins). One helper
    rather than one spelling per listing function: the widener alone could not
    express "archived only", and three listings inventing that answer three
    ways is how the next one gets it wrong.
    """
    if archived_only:
        return archived
    return include_archived or not archived


def iter_history_files() -> Iterator[Path]:
    """Yield every chat record file, in stem order.

    The stem is a uuid4, so that order carries no chronology — a caller that
    wants newest-first sorts the records it loads, it does not read the name.

    For consumers that want the files rather than parsed records — the
    work-index backfill is the one — so the directory keeps a single owner
    instead of growing a walk per caller.
    """
    directory = chats_dir()
    if not directory.exists():
        return
    yield from sorted(directory.glob("*.json"))


def _activity_key(record: ChatRecord) -> str:
    return record.ended_at or record.started_at or record.created_at or ""


def list_records(
    *,
    include_archived: bool = False,
    archived_only: bool = False,
    limit: int | None = None,
    touched_since: float | None = None,
) -> list[ChatRecord]:
    """Return whole records, most recently ACTIVE first.

    Sorted by ``ended_at``, falling back to ``started_at`` then ``created_at``.
    Activity rather than creation because the readers this serves — the chat
    digest, the feedback sweep — ask what happened lately. ``list_by_day``
    groups by ``created_at``. The two are different questions and neither
    answers the other.

    ``touched_since`` is a unix mtime cutoff applied to the file BEFORE it is
    parsed. Nothing prunes the chats directory, and the capture funnel reads
    it every five minutes, so parsing every file every tick would cost the
    install's entire history forever — growing with how long the operator has
    owned the app rather than with what they said today. A file older than the
    cutoff cannot carry a turn that pass would act on, so it is never opened.
    """
    records: list[ChatRecord] = []
    for path in iter_history_files():
        if touched_since is not None:
            try:
                if path.stat().st_mtime < touched_since:
                    continue
            except OSError:
                continue
        record = load_chat(path.stem)
        if record is None:
            continue
        if not _wanted(record.archived, include_archived, archived_only):
            continue
        records.append(record)
    records.sort(key=_activity_key, reverse=True)
    return records[:limit] if limit is not None else records


def list_by_day(
    *, include_archived: bool = False, archived_only: bool = False
) -> list[dict[str, Any]]:
    """Group chats by the day they were CREATED, newest day first.

    The shape ``session_store.list_sessions_by_day`` returned, with two
    deliberate differences. A run is keyed by ``chat_id`` rather than a
    filename stem, because that is the identity now; and it carries ``title``,
    because a uuid is not a label and the stem used to be one.

    There is no ``custom`` bucket. That existed only because an operator could
    name a file anything, leaving the day to be parsed back out of the name and
    sometimes failing. ``created_at`` is stamped once at creation and answers
    every time.

    Read from the derived index when it has rows, from the records on disk when
    it does not. The drawer opens this on every render and the disk read parses
    every transcript in full to use six header fields — a cost that grows with
    how long the operator has owned the app rather than with what is shown. The
    fallback is what makes the index safe to be derived: a fresh install, a
    deleted sqlite or a test fixture that never wrote one still lists.
    """
    headers = _index_headers(
        include_archived=include_archived, archived_only=archived_only
    )
    if headers is None:
        headers = [
            _header(record)
            for record in list_records(
                include_archived=include_archived, archived_only=archived_only
            )
        ]
    return _group_by_day(headers)


def _index_headers(
    *, include_archived: bool, archived_only: bool
) -> list[dict[str, Any]] | None:
    """The day view's rows from the index, or ``None`` to read the records.

    ``None`` when the index is unreachable, empty, or does not hold exactly one
    row per record file. That last check is what makes the fast path safe to
    trust: nothing rebuilds this index on a schedule, so a row that never
    arrived — a burst left uncommitted by a kill, a file dropped in by hand —
    would hide a conversation from the drawer indefinitely, and a fallback on
    an EMPTY result cannot see a listing that is merely short. Counting the
    files is a directory listing; the parse is what the index exists to avoid.
    """
    def _read(index: Any) -> tuple[int, list[dict[str, Any]]]:
        return index.count(), index.list_headers(
            include_archived=include_archived, archived_only=archived_only
        )

    rows, headers = _with_index(_read, (0, []))
    if not rows:
        return None
    on_disk = sum(1 for _ in iter_history_files())
    if rows != on_disk:
        logger.warning(
            "chat_metadata: %d rows against %d records — reading the records",
            rows, on_disk,
        )
        return None
    return headers


def _header(record: ChatRecord) -> dict[str, Any]:
    """The fields the day view needs, off a parsed record.

    Same keys the index returns, so the grouping below cannot tell the two
    sources apart — the retiring index re-implemented the whole day view
    instead, with a comment in each copy promising it matched the other.
    """
    return {
        "chat_id": record.chat_id,
        "title": record.title,
        "created_at": record.created_at or record.started_at or "",
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "turn_count": record.turn_count,
        "model": record.model,
    }


def _group_by_day(headers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for header in headers:
        day = (header.get("created_at") or header.get("started_at") or "")[:10]
        if len(day) != 10:
            # Not droppable in silence: a record the drawer cannot place is a
            # conversation the operator cannot reach.
            logger.warning(
                "chat %s has no usable date; omitted from days", header.get("chat_id")
            )
            continue
        by_day.setdefault(day, []).append({
            "chat_id": header["chat_id"],
            "title": header["title"],
            "started_at": header["started_at"],
            "ended_at": header["ended_at"],
            "turn_count": header["turn_count"],
            "model": header["model"],
        })
    days: list[dict[str, Any]] = []
    for day_key, runs in by_day.items():
        runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        days.append({
            "date": day_key,
            "runs": runs,
            "run_count": len(runs),
            "total_turns": sum(r.get("turn_count", 0) for r in runs),
        })
    days.sort(key=lambda d: d["date"], reverse=True)
    return days


def preview_chat(chat_id: str, max_turns: int = 6) -> dict[str, Any] | None:
    """First N user/assistant turns, text only. ``None`` if the chat is gone.

    What the drawer shows on hover, so it carries no tool calls and no
    reasoning blobs — and no `chat_id` beyond the one it was asked for, since
    the caller already has it.
    """
    record = load_chat(chat_id)
    if record is None:
        return None
    turns: list[dict[str, Any]] = []
    for msg in record.history:
        if len(turns) >= max_turns:
            break
        if msg.get("role") not in ("user", "assistant") or msg.get("_reasoning"):
            continue
        text = extract_message_text(msg.get("content"))
        if not text:
            continue
        turns.append({"role": msg.get("role"), "text": text[:600]})
    return {
        "chat_id": record.chat_id,
        "title": record.title,
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "turn_count": record.turn_count,
        "model": record.model,
        "turns": turns,
    }


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


def persist_session_chats(session: Any, *, skip_empty: bool = False, model: str = "") -> int:
    """Flush every chat in a live ``ServerSession`` to disk. Returns the count.

    Duck-typed (no import of ``ServerSession``) to keep this module free of a
    cycle: reads ``session.session_id`` / ``session.chats`` / ``session.chat_meta``.
    Persists open AND archived chats so archive state survives a restart. A
    single chat's failure is logged and skipped — one bad chat must not lose
    the others on session close.

    ``skip_empty`` omits chats with no history, for the periodic writer: an
    empty chat rewritten every interval is churn. Teardown leaves it False so
    archive state still reaches disk for a chat that was never typed in.

    ``model`` is the adapter's model for this session, which lives on the
    writers' ``opts`` rather than on the session. Passing it stamps the ACTIVE
    chat's meta; omitting it writes whatever each chat already carries. That
    asymmetry is the point — rename, archive and restore all persist without
    knowing the model, and a plain keyword defaulted to ``""`` would let any of
    them blank a record the autosave had just filled in.

    Only the active chat, because a session rehydrates every open conversation
    on connect and stamping them all would relabel a chat last held by another
    model with whatever is configured today — the digest reads this field, and
    it would report the wrong model for every old conversation the operator
    happened to have open.
    """
    saved = 0
    active_meta = session.chat_meta.get(getattr(session, "active_chat_id", ""))
    if model and active_meta is not None:
        active_meta.model = model
    with _index_batch():
        for chat_id, cs in dict(session.chats).items():
            meta = session.chat_meta.get(chat_id)
            if meta is None:
                continue
            if skip_empty and not getattr(cs, "history", None):
                continue
            try:
                save_chat(ChatRecord(
                    chat_id=chat_id,
                    session_id=session.session_id,
                    title=meta.title,
                    created_at=meta.created_at,
                    started_at=meta.started_at,
                    archived=meta.archived,
                    model=getattr(meta, "model", "") or "",
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


def archive_stale_open_chats(
    today: str | None = None, *, keep_days: int = 0
) -> int:
    """Auto-archive open chats whose last activity predates the window.

    Two callers, one rule. The day rollover leaves ``keep_days`` at 0, so the
    cutoff is today and anything last touched on an earlier day is archived:
    operator request (2026-07-05), a fresh Mirror connection on a new local
    calendar day should seed a blank chat rather than resume yesterday's
    thread, while cost/turns (already day-scoped elsewhere) reset in step.
    The retention sweep passes the window from ``retention.yaml``, where
    ``keep_days`` has always meant days since activity — a chat active inside
    it stays open.

    Called by ``chat_restore.py`` before it rebuilds the tab strip from disk —
    archiving (not deleting) means the stale chat stays fully reachable via
    ``GET /api/chats?include_archived=1`` and the ``chat.restore`` WS command,
    same as any operator-archived chat. A record with no parseable timestamp is
    left open (fail-safe, not fail-archive), and so is one stamped in the
    FUTURE: a clock that ran ahead is not a reason to shelve a conversation.
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
    anchor = date.fromisoformat(
        today or datetime.now().astimezone().date().isoformat()
    )
    cutoff = (anchor - timedelta(days=max(keep_days, 0))).isoformat()
    archived = 0
    for row in list_chats():
        record = load_chat(row["chat_id"])
        if record is None:
            continue
        last_active = _last_activity_date(record)
        if last_active is not None and last_active < cutoff:
            if set_archived(record.chat_id, True):
                archived += 1
    return archived


def delete_chat(chat_id: str) -> tuple[bool, str]:
    """Hard-delete a chat file and everything derived from it.

    Returns ``(ok, reason)``: ``(True, "")`` deleted; ``(False, "invalid_id")``
    malformed id; ``(False, "not_found")`` no such file; ``(False, "io_error")``
    unlink failed. Archive-before-delete policy (D1) is enforced by the route
    layer, not here.

    Four things outlive the file unless this function says otherwise, and each
    is reached by the id rather than searched for:

    - the outstanding completion, which nothing could ever claim again;
    - the derived index row, which the nightly sweep would otherwise carry
      until tomorrow;
    - the work-index chunks, and until this called ``delete_by_path``
      ``recall_history`` could still quote a conversation the operator had
      deleted — for up to a day;
    - the conversation's recap MEMORY, which is deliberately **not** deleted.
      What was learned outlives the transcript that taught it; what changes is
      that the record stops implying there is a transcript to go back to.
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
    # The chat_id is gone for good, so nothing can ever claim or replay a
    # completion still outstanding for it. Left behind it would sit on disk
    # forever (`brain/completion_store.py`).
    from tesseract.brain import completion_store

    completion_store.discard(chat_id)
    _with_index(lambda index: index.delete(chat_id), None)
    _forget_work_chunks(path)
    _mark_recap_source_deleted(chat_id)
    return True, ""


def _forget_work_chunks(path: Path) -> None:
    """Drop this record's chunks from the work index. Best-effort.

    The nightly ``work_index_sweep`` still prunes chunks whose source file is
    gone — that is the backstop for a delete that bypassed this function, not
    the reason recall is honest within the second.
    """
    try:
        from tesseract.memory.work_index import WorkIndex

        index = WorkIndex(_home() / "work_index.sqlite")
    except Exception:  # noqa: BLE001
        return
    try:
        index.delete_by_path(str(path))
    finally:
        try:
            index.close()
        except Exception:  # noqa: BLE001
            pass


def _mark_recap_source_deleted(chat_id: str) -> None:
    """Tell this chat's recap memory that its transcript is gone. Best-effort.

    Operator ruling (2026-08-19): the lesson persists and records that it was
    learned from a conversation that has since been deleted. Deleting the
    memory itself stays the operator's own act.
    """
    try:
        from tesseract.capture.reflect import mark_source_deleted
        from tesseract.capture.sources import MIRROR_SOURCE

        mark_source_deleted(f"{MIRROR_SOURCE}:{chat_id}")
    except Exception:  # noqa: BLE001 — a memory-store fault must not fail a delete
        logger.warning("chat delete: recap stamp failed for %s", chat_id, exc_info=True)
