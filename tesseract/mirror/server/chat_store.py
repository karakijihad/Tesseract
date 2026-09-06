"""Chat persistence — the one owner of the conversation record.

Every surface on the funnel reaches its history through this module: the
cockpit, a channel, the scheduled jobs, the retention sweep. The file layout
and the raw read/write are `chat_record.py`; the derived header index is
`chat_index.py`; the message-content helpers are `chat_content.py`. What lives
here is the API those consumers call and the rules a save has to keep — that a
turn count is derived rather than trusted, that a mutation writes through to
the index, and that a delete reaches everything the record fed.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tesseract.brain.chat import _starts_a_turn
from tesseract.mirror.server import chat_index, chat_record
from tesseract.mirror.server.chat_content import (
    extract_message_text,
    index_conversation_file,
    last_message_timestamp,
    sanitize_history_for_persistence,
)
from tesseract.mirror.server.chat_index import index_batch, rebuild_metadata_index
from tesseract.mirror.server.chat_record import (
    SCHEMA_VERSION,
    ChatRecord,
    chat_path,
    chats_dir,
    default_chat_title,
    is_valid_chat_id,
    iter_history_files,
    now_iso,
    write_record,
)

__all__ = [
    "SCHEMA_VERSION",
    "ChatRecord",
    "archive_stale_open_chats",
    "chats_dir",
    "delete_chat",
    "extract_message_text",
    "first_operator_text",
    "index_batch",
    "index_chat",
    "index_conversation_file",
    "index_session_chats",
    "iter_history_files",
    "last_message_timestamp",
    "list_chats",
    "list_records",
    "load_chat",
    "metadata_index_path",
    "persist_session_chats",
    "rebuild_metadata_index",
    "rename_chat",
    "sanitize_history_for_persistence",
    "save_chat",
    "set_archived",
]

logger = logging.getLogger(__name__)

#: Serialises every write to a chat record, across threads.
#:
#: Autosave runs its persist on a worker thread while the event loop keeps
#: serving; rename, archive and delete run on the loop. Both reach
#: ``atomic_write_text``, which is atomic per file and says nothing about two
#: writers, so without this a rename's read-modify-write and a periodic save of
#: the same chat can interleave and the later ``os.replace`` silently wins.
#: Re-entrant because the batch path takes it and then calls ``save_chat``,
#: which takes it again.
#:
#: The loop can wait on it, which is the cost. One chat's file is about a
#: millisecond, against the whole-tick block moving the write off the loop was
#: for.
_WRITE_LOCK = threading.RLock()


def metadata_index_path() -> Path:
    """Where the derived index lives. Delegated so there is one definition."""
    return chat_index.metadata_index_path()


def save_chat(record: ChatRecord) -> Path:
    """Persist a chat to ``chats/<chat_id>.json``.

    Sanitizes history (attachment bytes and reasoning items never reach disk),
    and derives BOTH ``turn_count`` and ``ended_at`` from the history so
    neither can drift from the saved messages. Raises ``ValueError`` on a
    malformed chat_id.

    A turn is counted the way the runtime counts one, by asking the runtime.
    Counting every ``role == "user"`` entry here meant a compacted chat's saved
    metadata reported one turn more than the session thought it had, because
    the summary a fold writes wears that role and opens nothing; a mid-turn
    injection lands inside a turn already open and is the same story.

    ``ended_at`` means when the conversation last CHANGED, so it is read off
    the last message rather than off the clock. Stamping ``now`` on every
    write made it mean "when this file was last written", which is not the
    same thing and is not what any reader wants: ``persist_session_chats``
    writes every chat a session holds whenever any ONE of them is saved, so a
    single autosave tick gave a dozen untouched conversations the same
    millisecond and every one of them looked like it had just happened.
    Measured on the operator's own library: nine records shared one stamp to
    the millisecond while their real last use was days apart.

    Deriving it makes the write idempotent — saving an unchanged history
    twice yields the same stamp — and costs nothing, because the value is
    already in the history being written. A conversation with no stamped
    message keeps whatever the caller carried, falling back to the clock so
    the field is never empty.
    """
    # Non-destructive: work on a copy so a caller that keeps using `record`
    # after the save doesn't find its history stripped / turn_count rewritten.
    record = replace(record)
    record.history = sanitize_history_for_persistence(record.history)
    record.turn_count = sum(1 for m in record.history if _starts_a_turn(m))
    with _WRITE_LOCK:
        record.ended_at = (
            last_message_timestamp(record.history) or record.ended_at or now_iso()
        )
        path = write_record(record)
        # Write-through to the derived index, so it stays current between
        # rebuilds. Every mutation the runtime makes to a record — autosave,
        # rename, archive, restore — lands here, which is why none of them
        # needs its own hook.
        #
        # Except a channel record. A reader on the index's fast path never
        # reaches `_walk`, so indexing one would route it straight past the
        # exclusion `_walk` enforces and into the operator's chat library. The
        # index is a picture of that library; a channel record is the bridge's
        # own restore state and is not in it.
        if record.surface != "channel":
            chat_index.upsert(record, path)
    return path


def archive_copy(record: ChatRecord | None, history: list[dict[str, Any]]) -> str | None:
    """Write what a conversation is leaving behind as its own archived record.

    The one place a boundary copies a transcript aside, called by both
    surfaces. It was two functions for a day, one reading the history from
    disk and one taking it in hand, and the disk one could archive a record up
    to a full autosave interval stale — missing exactly the exchange whose
    boundary this is.

    So the HISTORY is the argument and `record` only supplies the metadata. A
    missing record is not a reason to lose the conversation: the copy is
    written with whatever is known and an empty title, which is a conversation
    the operator can still read.

    Returns the new id, or `None` when there was nothing to copy or the copy
    could not be written. `None` means the caller must NOT wipe: without the
    copy, wiping in place is deleting.
    """
    if not history:
        return None
    copy_id = uuid.uuid4().hex
    try:
        save_chat(ChatRecord(
            chat_id=copy_id,
            session_id=getattr(record, "session_id", "") or "",
            title=getattr(record, "title", "") or "",
            created_at=getattr(record, "created_at", "") or now_iso(),
            started_at=getattr(record, "started_at", "") or now_iso(),
            history=list(history),
            archived=True,
            model=getattr(record, "model", "") or "",
            surface=getattr(record, "surface", "cockpit") or "cockpit",
        ))
        index_chat(copy_id)
    except Exception:
        logger.exception("archive_copy: could not write what %s is leaving", copy_id)
        return None
    return copy_id


def load_chat(chat_id: str, *, include_channels: bool = False) -> ChatRecord | None:
    """Load a chat, or None if missing / unreadable / invalid id.

    ``include_channels`` mirrors ``_walk``'s, and for the same reason. Keeping
    channel records out of the LISTINGS was not enough on its own: a durable
    channel id is derived deterministically from the channel and the chat
    (`_channel_session.durable_chat_id`), so anything holding a chat id can
    compute one rather than having to be shown it. The bridge asks for its own
    record by name; every other reader gets None.
    """
    record = chat_record.read_record(chat_id)
    if record is None:
        return None
    if not include_channels and record.surface == "channel":
        return None
    return record


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


#: How much of the first message a rail row can use. The row ellipsizes at
#: whatever width it has, so this is only a bound on what travels.
SNIPPET_CHARS = 120


def first_operator_text(record: ChatRecord) -> str:
    """The first thing the operator typed into this chat, bounded.

    What a row shows when the title is still the stamp the chat was born
    with. Messages the runtime wrote itself are skipped, so no sentence the
    operator never typed can become a conversation's name: a folded-context
    block, a wake nudge, a heartbeat report, a card press or the reflection
    prompt. Each carries the mark `ChatSession` stamps on its own messages
    (`brain/chat.py::_RUNTIME_KEY`), and none is matched on its text, which
    ships in a public repo and could therefore be typed by anyone.
    """
    for msg in record.history:
        if msg.get("role") != "user":
            continue
        if msg.get("_runtime"):
            continue
        text = " ".join(extract_message_text(msg.get("content")).split())
        if text:
            return text[:SNIPPET_CHARS]
    return ""


def list_chats(
    *, include_archived: bool = False, archived_only: bool = False
) -> list[dict[str, Any]]:
    """Return sidebar metadata rows (no history), newest-created first.

    ``snippet`` is present only when the title is still the birth stamp, so a
    reader needs no second rule to know which of the two to show: an operator
    rename outranks it by the snippet not being sent at all.

    ``last_active_at`` is when the conversation was last used, which is what
    the rail files a row under: a chat picked up again today belongs under
    today, not under the month it was started in. The rows still come back
    newest-CREATED first, because ``chat_restore`` takes the head of this list
    to decide which conversations a connection hydrates.
    """
    rows: list[dict[str, Any]] = []
    for record in _walk(include_archived=include_archived, archived_only=archived_only):
        born = default_chat_title(record.created_at or "")
        rows.append({
            "chat_id": record.chat_id,
            "title": record.title,
            "snippet": first_operator_text(record) if record.title == born else "",
            "created_at": record.created_at,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "last_active_at": _last_active_stamp(record),
            "turn_count": record.turn_count,
            "model": record.model,
            "archived": record.archived,
            "message_count": len(record.history),
        })
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def _walk(
    *,
    include_archived: bool,
    archived_only: bool,
    touched_since: float | None = None,
    include_channels: bool = False,
) -> Iterator[ChatRecord]:
    """Every record the filters want, parsed once. The only walk of the tree.

    ``include_channels`` defaults to FALSE, and that default is load-bearing.
    A channel record exists so a bridge can restore its own conversation after
    a restart; it is addressed by its durable id and read by nothing else.
    Left visible to this walk it reaches two places it must not:

    - the cockpit's restore-on-connect (`chat_restore._restore_persisted_chats`
      takes the newest rows and opens them), which would load someone else's
      Telegram thread into the operator's window and then index it into the
      install-wide recall index when that connection closes. `recall_history`
      searches that index with no owner filter, so a second approved Telegram
      user could then ask for, and receive, excerpts of the first one's
      conversation.
    - the capture funnel, which already recaps a channel from the channel's own
      conversation store. Counting the record too would recap it twice.

    A caller that genuinely wants channel records asks for them by name.
    """
    for path in iter_history_files():
        if touched_since is not None:
            try:
                if path.stat().st_mtime < touched_since:
                    continue
            except OSError:
                continue
        record = chat_record.read_record(path.stem)
        if record is None:
            continue
        if not include_channels and record.surface == "channel":
            continue
        if not _wanted(record.archived, include_archived, archived_only):
            continue
        yield record


def _activity_key(record: ChatRecord) -> str:
    return record.ended_at or record.started_at or record.created_at or ""


def list_records(
    *,
    include_archived: bool = False,
    archived_only: bool = False,
    limit: int | None = None,
    touched_since: float | None = None,
    include_channels: bool = False,
) -> list[ChatRecord]:
    """Return whole records, most recently ACTIVE first.

    Sorted by ``ended_at``, falling back to ``started_at`` then ``created_at``.
    ``save_chat`` derives ``ended_at`` from the last message, so this really is
    when the conversation last changed; while it was stamped from the clock,
    one autosave tick made every chat a session held look equally recent and
    this order was close to meaningless.
    Activity rather than creation because the readers this serves — the chat
    digest, the feedback sweep — ask what happened lately. ``list_chats``
    sorts by ``created_at``. The two are different questions and neither
    answers the other.

    ``include_channels`` is off by default and the reason is in ``_walk``. A
    channel record is the bridge's own restore state, addressed by durable id;
    a reader that genuinely wants one says so.

    ``touched_since`` is a unix mtime cutoff applied to the file BEFORE it is
    parsed. Nothing prunes the chats directory, and the capture funnel reads
    it every five minutes, so parsing every file every tick would cost the
    install's entire history forever — growing with how long the operator has
    owned the app rather than with what they said today. A file older than the
    cutoff cannot carry a turn that pass would act on, so it is never opened.
    """
    records = list(_walk(
        include_archived=include_archived,
        archived_only=archived_only,
        include_channels=include_channels,
        touched_since=touched_since,
    ))
    records.sort(key=_activity_key, reverse=True)
    return records[:limit] if limit is not None else records


def set_archived(chat_id: str, archived: bool = True) -> bool:
    """Flip a chat's archived flag on disk. Returns False if it doesn't exist.

        Reads and writes under one lock: the flag is derived from what is on disk,
        and a save landing in between would be written from a record that predates
        this one."""
    with _WRITE_LOCK:
        record = load_chat(chat_id)
        if record is None:
            return False
        record.archived = archived
        save_chat(record)
    return True


def rename_chat(chat_id: str, title: str) -> bool:
    """Set a chat's operator title. Returns False if it doesn't exist.

        Read and write under one lock, for the reason ``set_archived`` gives: a
        concurrent save would otherwise carry the title this call replaced."""
    with _WRITE_LOCK:
        record = load_chat(chat_id)
        if record is None:
            return False
        record.title = title
        save_chat(record)
    return True


def _is_disposable(meta: Any, chat: Any, chat_id: str) -> bool:
    """True for a chat that holds nothing worth a file.

    A session seeds a blank chat on every connection, and a connection the
    operator never typed into leaves that blank behind on teardown. Three
    conditions together mean there is nothing to keep: no messages, no
    operator rename (the title is still the birth timestamp), and not
    archived. Any one of them failing is state a restart must not lose, so
    the record is written: an archived blank remembers that it was shelved,
    and a named blank remembers what the operator meant to use it for.

    And a chat that already has a record is never disposable, whatever shape
    it is in now. The question this answers is "is there anything to write?",
    never "is there anything to keep": a cleared chat looks exactly like a
    blank one, so without this the write was skipped and the transcript the
    operator asked to be gone stayed on disk. Skipping a write only ever
    avoids making a file; once one exists, skipping preserves its contents.
    """
    if getattr(chat, "history", None):
        return False
    if getattr(meta, "archived", False):
        return False
    born = default_chat_title(getattr(meta, "created_at", "") or "")
    if not (born and getattr(meta, "title", "") == born):
        return False
    return not (is_valid_chat_id(chat_id) and chat_path(chat_id).exists())


def persist_session_chats(session: Any, *, skip_empty: bool = False, model: str = "") -> int:
    """Flush every chat in a live ``ServerSession`` to disk. Returns the count.

    Duck-typed (no import of ``ServerSession``) to keep this module free of a
    cycle: reads ``session.session_id`` / ``session.chats`` / ``session.chat_meta``.
    Persists open AND archived chats so archive state survives a restart. A
    single chat's failure is logged and skipped — one bad chat must not lose
    the others on session close.

    ``skip_empty`` omits chats with no history, for the periodic writer: an
    empty chat rewritten every interval is churn. Teardown leaves it False,
    because archive state belongs on disk for a chat that was never typed in.
    A blank that is neither archived nor renamed carries no such state and is
    dropped either way, by ``_is_disposable`` — a session seeds one on every
    connection, so persisting them left a dead file per connection.

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
    # Held across the whole batch, not per chat: this runs on autosave's worker
    # thread, and a rename landing between two chats of one tick would be read
    # back by a later `load_chat` in the same pass.
    with _WRITE_LOCK, index_batch():
        for chat_id, cs in dict(session.chats).items():
            meta = session.chat_meta.get(chat_id)
            if meta is None:
                continue
            if skip_empty and not getattr(cs, "history", None):
                continue
            if _is_disposable(meta, cs, chat_id):
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
                    # The door this conversation came through. `ServerSession.
                    # kind` already carries it, so a channel chat lands in the
                    # same store as a cockpit one and stays tellable apart.
                    surface=getattr(session, "kind", "") or "cockpit",
                    history=list(getattr(cs, "history", []) or []),
                ))
                saved += 1
            except Exception:  # noqa: BLE001 — never lose other chats on one failure
                logger.exception("persist_session_chats: failed for chat %s", chat_id)
    return saved


def index_chat(chat_id: str) -> bool:
    """Re-index one chat's record so recall says what the record says.

    ``index_conversation_file`` deletes every chunk for the path before adding
    from the file, so this is how a record that LOST content loses its chunks
    too — an emptied record leaves none behind. The record on disk is what
    decides, never the live chat: a cleared conversation has no history to
    index and is exactly the case that has to reach the indexer.
    """
    if not is_valid_chat_id(chat_id):
        return False
    path = chat_path(chat_id)
    if not path.exists():
        return False
    try:
        index_conversation_file(path)
    except Exception:  # noqa: BLE001 — never block a caller on one chat's indexer
        logger.exception("index_chat: failed for chat %s", chat_id)
        return False
    return True


def index_session_chats(session: Any) -> int:
    """Index every persisted chat into the work index for recall.

    Each chat is indexed by its own ``sessions/chats/<chat_id>.json`` file, so
    ``recall_history`` surfaces background chats too — not just whichever chat
    was active at close. Best-effort and duck-typed (reads ``session.chats``);
    a single chat's failure is logged and skipped. Returns the count indexed.

    Call AFTER ``persist_session_chats`` so the files exist on disk.
    """
    return sum(1 for chat_id in dict(session.chats) if index_chat(chat_id))


def _last_active_stamp(record: ChatRecord) -> str:
    """ISO stamp of when this chat was last actually used, or "".

    The last message's own timestamp first, then the record-level
    ``ended_at``/``started_at``/``created_at``. ``save_chat`` now derives
    ``ended_at`` from that same last message, so the first two rungs agree
    for any conversation that has one; the order is kept because the message
    is the source and the field is the copy, and a record written by an older
    build carries a copy that was stamped from the clock.

    Two readers: the retention sweep, which wants the calendar date, and the
    conversations rail, which files a row under the day it was last used
    rather than the day it was born. One rule so the two cannot disagree.
    """
    return (
        last_message_timestamp(record.history)
        or record.ended_at
        or record.started_at
        or record.created_at
        or ""
    )


def _last_activity_date(record: ChatRecord) -> str | None:
    """Local calendar date (``YYYY-MM-DD``) this chat was last actually used.

    Message timestamps may be UTC while record fields are local-zone;
    ``.astimezone()`` normalizes either to the machine's local calendar date.
    Returns None when nothing parses — callers treat that as "don't know,
    leave it alone" rather than guessing.
    """
    stamp = _last_active_stamp(record)
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).astimezone().date().isoformat()
    except ValueError:
        return None


def archive_stale_open_chats(today: str | None = None, *, keep_days: int) -> int:
    """Auto-archive open chats whose last activity predates the window.

    One caller: the retention sweep, passing the window from
    ``retention.yaml::sessions``, where ``keep_days`` has always meant days
    since activity. A chat active inside it stays open.

    **``keep_days`` has no default any more, and that is the point.** It used
    to default to 0, and the day-rollover restore called it with nothing, so a
    conversation untouched since yesterday was shelved on the next connect and
    had to be restored by hand. IS-18 removed that call on the operator's
    instruction; requiring the argument is what stops it coming back by
    accident, because 0 means "archive everything not touched today" and no
    caller should be able to ask for that without typing it.

    Archiving is not deleting: the chat stays reachable through
    ``GET /api/chats?include_archived=1`` and ``chat.restore``. A record with
    no parseable timestamp is left open (fail-safe, not fail-archive), and so
    is one stamped in the FUTURE: a clock that ran ahead is not a reason to
    shelve a conversation. Returns the count archived.

    Known limitation: this reads/writes the global on-disk chat library with
    no cross-connection lock. Two concurrent sweeps could interleave. Rare,
    left unhandled; anything that hits it can re-fetch via ``GET /api/chats``.
    """
    anchor = date.fromisoformat(
        today or datetime.now().astimezone().date().isoformat()
    )
    cutoff = (anchor - timedelta(days=max(keep_days, 0))).isoformat()
    archived = 0
    for record in _walk(include_archived=False, archived_only=False):
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
    if not is_valid_chat_id(chat_id):
        return False, "invalid_id"
    path = chat_path(chat_id)
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
    chat_index.forget(chat_id)
    _forget_work_chunks(path)
    _mark_recap_source_deleted(chat_id)
    return True, ""


def _forget_work_chunks(path: Path) -> None:
    """Drop this record's chunks from the work index. Best-effort.

    The nightly ``work_index_sweep`` still prunes chunks whose source file is
    gone — that is the backstop for a delete that bypassed this function, not
    the reason recall is honest within the second.
    """
    from tesseract.paths import home_dir

    try:
        from tesseract.memory.work_index import WorkIndex

        index = WorkIndex(home_dir() / "work_index.sqlite")
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
