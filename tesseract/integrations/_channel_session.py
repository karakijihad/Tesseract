"""How a channel session is bounded, and the one boundary a channel owns.

**Channel-agnostic on purpose.** Telegram is an adapter, not the feature —
WhatsApp, Instagram, an email lane and whatever comes next reach the same
runtime through the same funnel, so anything true of "a chat" belongs here and
not in one bridge. An adapter supplies its own transport (how to send a line);
everything about what a session IS lives in this module.

Two things live here, and nothing else does:

- **Compaction after a turn**, which is the cockpit's own hook. `turn_runner`
  calls `auto_compact_if_needed` after every turn it drives; a channel turn
  goes through `channel_turn` instead and used to be bounded by a 20-turn
  sliding window of its own. That window was a second policy for a problem the
  runtime already solved, and it made a channel a smaller assistant rather than
  a narrower pipe. It is gone.
- **The day boundary**, which is the one thing a channel does have that a
  cockpit does not: no visible "new chat" button. So the first message of a new
  local day is OFFERED a fresh session. Never given one — the reset it replaces
  wiped six hours of silence without asking and without saying it had.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger(__name__)

#: What the operator sees on the first message of a new day, after their
#: message has been answered. Named `/clear` because that is the command the
#: channels actually have; there is no `/reset`.
NEW_DAY_OFFER = (
    "First message of a new day — this is still yesterday's session. "
    "Send /clear to start fresh, or just keep going."
)


class ChatMemoryLike(Protocol):
    """The per-chat rolling summary, as this module needs it."""

    def append_evictions(
        self, channel: str, chat_id: str, rows: list[dict[str, Any]]
    ) -> None: ...


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


async def compact_after_turn(
    chat_session: Any,
    *,
    chat_memory: ChatMemoryLike | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
) -> None:
    """Bound the live history the way the cockpit does, and no other way.

    The rows compaction removes are forwarded to the rolling per-chat summary.
    Compaction leaves its own running summary inside the history, which carries
    the SESSION; the chat summary is what carries the CHAT across sessions, so
    it must not starve just because the trimmer left.

    Never raises: the turn has already landed and been sent.
    """
    from tesseract.brain.session_ops import auto_compact_if_needed

    prior = list(getattr(chat_session, "history", []) or [])
    try:
        result = await auto_compact_if_needed(chat_session)
    except Exception:
        log.exception("channel session: compaction failed for %s/%s", channel, chat_id)
        return
    if result is None:
        return
    before, after = result
    log.info(
        "channel session: compacted %s/%s — %d → %d tokens",
        channel, chat_id, before, after,
    )
    if chat_memory is None or channel is None or chat_id is None:
        return
    kept = {id(row) for row in getattr(chat_session, "history", []) or []}
    evicted = [
        row for row in prior
        if id(row) not in kept
        and not (isinstance(row, dict) and row.get("role") == "system")
    ]
    if not evicted:
        return
    try:
        chat_memory.append_evictions(channel, str(chat_id), evicted)
    except Exception:
        log.exception(
            "channel session: append_evictions failed for %s/%s", channel, chat_id,
        )


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
