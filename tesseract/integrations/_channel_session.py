"""How a channel session is bounded, and the one boundary a channel owns.

**Channel-agnostic on purpose.** Telegram is an adapter, not the feature —
WhatsApp, Instagram, an email lane and whatever comes next reach the same
runtime through the same funnel, so anything true of "a chat" belongs here and
not in one bridge. An adapter supplies its own transport (how to send a line);
everything about what a session IS lives in this module.

Three things live here, and nothing else does:

- **The day boundary**, which is the one thing a channel does have that a
  cockpit does not: no visible "new chat" button. So the first message of a new
  local day is OFFERED a fresh session. Never given one — taking it would wipe
  a night of silence without asking and without saying so.
- **The durable record**, which is the cockpit's own store. A channel chat IS
  a chat: same `sessions/chats/<id>.json`, same autosave timer, same restore.
  What it needed was an id that survives a restart, because a bridge mints a
  new session on every boot and a record nothing can address again is a record
  nothing reads. It is NOT listed to the generic readers — `chat_store._walk`
  excludes it unless a caller asks by name — because those feed the cockpit's
  restore-on-connect and the install-wide recall index, and a chat can carry
  someone who is not the operator. The bridge reaches its own record by id;
  what a reader searches is the day log under `logs/channels/`.
- **The identity stamp**, which is the set of ids a tool needs in order to
  tell whose conversation it is running in. Three of them, each answering a
  different question, and a bridge that set two shipped a runtime that could
  not find a photo the operator had just sent.

Compaction used to live here too. It is one moment in the runtime, not a
channel rule, so it moved to `mirror/server/after_turn.py` where the cockpit
reaches the same function. The bridge calls it directly.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

#: What the operator sees on the first message of a new day, after their
#: message has been answered. Named `/clear` because that is the command the
#: channels actually have; there is no `/reset`.
NEW_DAY_OFFER = (
    "First message of a new day — this is still yesterday's session. "
    "Send /clear to start fresh, or just keep going."
)


#: Namespace for the per-chat durable id. Any fixed uuid works; what matters
#: is that it never changes, because changing it orphans every record already
#: written under the old one.
_CHANNEL_CHAT_NS = uuid.UUID("6f8f4c5e-2c1a-4f9b-9a3d-7b0c5e1d4a20")


def durable_chat_id(channel: str, chat_id: str) -> str:
    """The one chat record id for this conversation, stable across restarts.

    A bridge builds a fresh `ServerSession` on every boot and on every
    `/clear`, so a random id would write a new record each time and no restart
    could ever find the last one. Derived from the channel and the chat, so
    the same phone reaching the same bot is the same conversation on disk.

    32 lowercase hex, which is what `chat_record.is_valid_chat_id` accepts.
    """
    return uuid.uuid5(_CHANNEL_CHAT_NS, f"{channel}:{chat_id}").hex


def stamp_identity(
    chat_session: Any,
    session: Any,
    *,
    channel: str,
    chat_id: str,
    durable_id: str,
) -> None:
    """Say whose conversation this is, once, for every channel there will be.

    Three ids reach the runtime here and none of them substitutes for another:

    - ``tool_context.chat_id`` is the DURABLE record id. History reads it, and
      a tool checks its own ``chat_ref`` argument against it.
    - ``tool_context.channel`` says which door the chat came through, because
      the id alone cannot. A cockpit chat carries a ``chat_id`` too, so a guard
      reading only that one fires on the operator's own window.
    - ``tool_context.channel_chat_id`` is the ADAPTER's id for the chat, and
      the only one that finds what the bridge filed under it: inbound media
      lives at ``uploads/channels/<channel>/<chat_id>/``, which the durable id
      matches no directory in. ``session.channel_chat_id`` is the same value,
      read by the adapter when it needs a send address.

    Stamping two of the three is not a partial success, it is a runtime where
    a photo the operator sent a minute ago cannot be found. So all four
    assignments happen together and no adapter gets to choose.
    """
    try:
        chat_session.tool_context.chat_id = durable_id
        chat_session.tool_context.channel = channel
        chat_session.tool_context.channel_chat_id = chat_id
    except AttributeError:
        log.debug("channel session: could not stamp tool_context identity")
    setattr(session, "channel_chat_id", chat_id)


def restore_meta(session: Any, durable_id: str, record: Any | None = None) -> None:
    """Put the record's own title and dates back on the live chat meta.

    `ServerSession.__post_init__` stamps a fresh `ChatMeta` with the current
    time whenever it mints a chat, which on a channel is every bridge boot.
    Restoring only the history left that meta in place, and the first save
    afterwards wrote restart-time values over the record's real ones — so a
    conversation running for a week reported itself as created minutes ago,
    every week. The cockpit's own restore rebuilds the meta from the record
    (`chat_restore._restore_persisted_chats`); this is the same act.
    """
    from tesseract.mirror.server import chat_store

    if record is None:
        try:
            record = chat_store.load_chat(durable_id, include_channels=True)
        except Exception:
            return
    if record is None:
        return
    meta = (getattr(session, "chat_meta", None) or {}).get(durable_id)
    if meta is None:
        return
    if record.title:
        meta.title = record.title
    if record.created_at:
        meta.created_at = record.created_at
    if record.started_at:
        meta.started_at = record.started_at
    meta.turn_count = record.turn_count


def restore_history(
    chat_session: Any, durable_id: str, out: list[Any] | None = None,
) -> int:
    """Seed a freshly built channel session from its record. Returns turns.

    This is the half that makes autosave worth having: writing every turn to
    disk buys nothing if the next boot starts empty anyway. Best-effort — a
    missing or unreadable record is a new conversation, which is exactly what
    a fresh session already is.
    """
    from tesseract.mirror.server import chat_store

    try:
        record = chat_store.load_chat(durable_id, include_channels=True)
    except Exception:
        log.exception("channel session: could not read record %s", durable_id)
        return 0
    if record is None:
        return 0
    # Handed back so `restore_meta` does not open the same file again. The two
    # halves have to describe the same conversation, and reading twice is how
    # they could come to disagree.
    if out is not None:
        out.append(record)
    if not record.history:
        return 0
    chat_session.history = list(record.history)
    # The observer's watermark is stamped when a session ATTACHES, and a
    # session attaches while it is being built — before this runs, with an
    # empty history, so the mark is 0. Left there, the first turn after a
    # restart hands the observer the entire restored conversation as "new
    # turns", which is the exact flood `_notify_observer_turn_end` says the
    # watermark exists to prevent. Restored turns were already lived through.
    try:
        chat_session._observer_last_index = len(chat_session.history)
    except Exception:
        log.exception("channel session: could not stamp observer watermark")
    # Same reason `chat_restore` does it: a spawn started under the previous
    # process has no surviving task here, and left unmarked it is a handle the
    # assistant will wait on forever.
    mark = getattr(chat_session, "mark_vanished_spawns", None)
    if callable(mark):
        try:
            mark(record.session_id)
        except Exception:
            log.exception("channel session: mark_vanished_spawns failed")
    # The other half of what `chat_restore` does after marking vanished
    # spawns. Work that finished while the bridge was down has a durable
    # completion waiting under this id, and without this it is never handed
    # over — the operator asked for something, it completed, and the answer
    # sits on disk unread.
    replay = getattr(chat_session, "replay_undelivered_completions", None)
    if callable(replay):
        try:
            replay(durable_id)
        except Exception:
            log.exception("channel session: completion replay failed")
    log.info(
        "channel session: restored %d message(s) from %s",
        len(record.history), durable_id,
    )
    return len(record.history)


def archive_record(durable_id: str, history: list[dict[str, Any]]) -> str | None:
    """Copy this conversation into its own archived record, before it is wiped.

    A boundary the agent reached is not the operator saying they want the
    conversation gone. `drop_record` is right for `/clear`, which is that; it
    is wrong for a consolidation, where the work carries on and being able to
    look up what was already said is the point.

    The HISTORY is passed in rather than read back off disk. A channel session
    is written by a periodic autosave, so the record on disk can be up to a
    full interval stale, which is exactly long enough to be missing the
    exchange whose boundary this is. The record is read only for its metadata,
    and a missing one is not a reason to lose the conversation.

    Returns the new id, or `None` when there was nothing to copy or the copy
    could not be written, in which case the caller must not wipe.
    """
    from tesseract.mirror.server import chat_store

    record = chat_store.load_chat(durable_id, include_channels=True)
    return chat_store.archive_copy(record, history)


def drop_record(durable_id: str) -> bool:
    """Delete this conversation's chat record. Returns whether one went.

    `/clear` has to mean cleared. The durable id is derived from the chat, so
    a session rebuilt under it would restore exactly the history the operator
    just asked to be rid of, and the command would silently do nothing.

    Deleted rather than archived aside, because the cockpit already settled
    what clear means: "the operator saying they want this conversation gone
    ... not that a copy survives somewhere they cannot see". The day-by-day
    transcript under `logs/channels/` is untouched and is what `/clear`
    already promised would still be there.
    """
    from tesseract.mirror.server import chat_store

    try:
        ok, _reason = chat_store.delete_chat(durable_id)
    except Exception:
        log.exception("channel session: could not drop record %s", durable_id)
        return False
    if ok:
        log.info("channel session: dropped record %s on clear", durable_id)
    return ok


def is_new_local_day(
    last_message_iso: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    """``True`` iff the last message fell on an earlier LOCAL calendar day.

    Replaces an inactivity window measured in minutes. A day is a day: six
    hours of silence over one evening is the same conversation, and a message
    sent this morning after one sent last night is not, however little time
    separated them.

    Local rather than UTC because the boundary has to be the one the person on
    the other end of the chat is living in.

    ``None`` / unparseable returns ``False``: a chat with no prior message has
    no boundary to have crossed.
    """
    if not last_message_iso:
        return False
    try:
        last = datetime.fromisoformat(last_message_iso)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return last.astimezone().date() < current.astimezone().date()


async def offer_a_fresh_session(
    *,
    crossed_into_a_new_day: bool,
    chat_session: Any,
    send: Callable[[str], Awaitable[None]],
) -> None:
    """Offer a reset on the first message of a new day. Never take one.

    ``send`` is the adapter's own outbound — the only channel-specific thing
    this needs, and the reason it is a parameter rather than an import.

    Sent after the reply so the message they wrote is answered first, and only
    when there is a session worth keeping: a chat with no history has nothing
    to offer clearing.
    """
    if not crossed_into_a_new_day:
        return
    if not getattr(chat_session, "history", None):
        return
    try:
        await send(NEW_DAY_OFFER)
    except Exception:
        log.exception("channel session: new-day offer failed")
