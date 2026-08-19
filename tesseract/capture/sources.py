"""Every entry point, read into one shape.

A source answers one question: which conversations exist on this machine, and
what was said in each. It answers it from disk rather than from a live adapter,
because a conversation that went quiet is exactly the case where the process
that held it may be gone — the old channel sweep read the bridge's in-memory
poll state and so had nothing to sweep after a restart.

`Turn` is deliberately poorer than either store's own row: a role, the text and
when it was said. Two conversations that carried the same words produce the
same record whichever entry point they arrived through, and that is what makes
`source` the only difference between them.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from tesseract.paths import log_dir

log = logging.getLogger(__name__)

# What the Mirror's own chats are tagged as. Channels name themselves —
# `channel:telegram`, later `email:…` — so the entry point is readable in the
# record without a lookup table.
MIRROR_SOURCE = "chat"


@dataclass(frozen=True)
class Turn:
    """One thing said, by one side."""

    role: str  # "user" | "assistant"
    text: str
    at: datetime


@dataclass(frozen=True)
class Conversation:
    source: str
    conversation_id: str
    turns: tuple[Turn, ...]

    @property
    def last_turn_at(self) -> datetime:
        return self.turns[-1].at

    @property
    def key(self) -> str:
        return f"{self.source}:{self.conversation_id}"


Collector = Callable[..., list[Conversation]]


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _text_of(content: Any) -> str:
    """The words in a history row, whatever shape it arrived in.

    Multimodal turns carry a list of blocks; only their text belongs in a
    recap, and an image block contributes nothing a reader could quote back.
    """
    if isinstance(content, list):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("text")
        ]
        return " ".join(p for p in parts if p).strip()
    return str(content or "").strip()


def _ordered(turns: Iterable[Turn]) -> tuple[Turn, ...]:
    return tuple(sorted(turns, key=lambda t: t.at))


def mirror_chats(tail_turns: int, *, lookback_hours: int) -> list[Conversation]:
    """The cockpit's own chats, from `sessions/chats/`.

    The chat files are what a reconnect reads, so they are current for a chat
    the operator is still in as well as one they closed — the autosave pump
    writes them while the session runs.

    **The identity is the chat's own, and deliberately.** `record.chat_id` is a
    uuid4 assigned when the chat is created and stable for its life, so a recap
    keyed off it names the same conversation forever. A filename minted from the
    clock on every connection is not identity and is not read here at all.

    **The chat store does the walking.** This used to glob the directory itself
    — the sixth such walk, which `session-record.md` forbids because the point
    is one owner of the record rather than one directory. `list_records` is
    that owner's listing, and `touched_since` is the question this collector
    actually asks.

    **Filtered by mtime before anything is parsed**, which `touched_since`
    preserves. `load_chat` reads and parses a chat's WHOLE history, this runs
    every five minutes, and nothing prunes `sessions/chats/` — so parsing every
    file every tick would cost the install's entire history forever, growing
    with how long the operator has owned the app rather than with what they
    said today. The channel side never had this shape: `ConversationStore.tail`
    reads only the newest per-day files. A file untouched for longer than the
    lookback cannot produce a conversation this pass would act on, so it is not
    opened.

    Archived chats are included: archiving is a shelf, not a retraction, and a
    chat archived while it was still being spoken in has turns worth recapping.
    """
    from tesseract.mirror.server import chat_store

    out: list[Conversation] = []
    try:
        records = chat_store.list_records(
            include_archived=True,
            touched_since=time.time() - lookback_hours * 3600,
        )
    except OSError:
        log.exception("capture: could not list the chats directory")
        return out
    for record in records:
        turns: list[Turn] = []
        for row in record.history:
            role = str(row.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            at = _parse_ts(row.get("timestamp"))
            text = _text_of(row.get("content"))
            if at is None or not text:
                continue
            turns.append(Turn(role=role, text=text, at=at))
        if not turns:
            # A chat whose turns predate per-message timestamps has no
            # timeline, so nothing can say whether it went quiet.
            continue
        out.append(
            Conversation(
                source=MIRROR_SOURCE,
                conversation_id=record.chat_id,
                turns=_ordered(turns[-tail_turns:]),
            )
        )
    return out


def _channel_chat_dirs() -> list[tuple[str, str]]:
    """`(channel, chat_id)` for every chat the channels tree knows about.

    Guarded the way the Mirror side's listing is: an unreadable directory costs
    this source, and the job's `return_exceptions=True` would then let it cost
    the other source's recaps too by reading as a wholly unreadable collector.
    One channel that cannot be walked is not the same as no channels.
    """
    root = log_dir("channels")
    if not root.exists():
        return []
    found: list[tuple[str, str]] = []
    try:
        channel_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        log.exception("capture: could not list the channels directory")
        return found
    for channel_dir in channel_dirs:
        try:
            chat_dirs = sorted(p for p in channel_dir.iterdir() if p.is_dir())
        except OSError:
            log.exception("capture: could not list %s", channel_dir.name)
            continue
        for chat_dir in chat_dirs:
            found.append((channel_dir.name, chat_dir.name))
    return found


def channel_chats(tail_turns: int, *, lookback_hours: int) -> list[Conversation]:
    """Every channel conversation, from the per-chat conversation store.

    The store is walked rather than the registered adapters: a chat held over
    Telegram is still a conversation this machine had when the bridge is not
    running, and the recap it earns does not depend on which process is up.

    `tail` is already bounded — it reads the newest per-day files until it has
    `limit` rows — so the lookback here only avoids opening a chat whose newest
    DAY FILE is already older than the window. The day is the filename, so that
    costs a listing rather than a read.
    """
    from tesseract.integrations._conversation_store import ConversationStore

    store = ConversationStore()
    oldest_day = (
        datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    ).date()
    out: list[Conversation] = []
    for channel, chat_id in _channel_chat_dirs():
        if not _touched_since(channel, chat_id, oldest_day):
            continue
        try:
            rows = store.tail(channel, chat_id, limit=tail_turns)
        except OSError:
            log.exception("capture: could not read %s/%s", channel, chat_id)
            continue
        turns: list[Turn] = []
        for row in rows:
            at = _parse_ts(row.get("ts"))
            text = str(row.get("body") or "").strip()
            if at is None or not text:
                continue
            role = "user" if row.get("direction") == "inbound" else "assistant"
            turns.append(Turn(role=role, text=text, at=at))
        if not turns:
            continue
        out.append(
            Conversation(
                source=f"channel:{channel}",
                conversation_id=chat_id,
                turns=_ordered(turns),
            )
        )
    return out


def _touched_since(channel: str, chat_id: str, oldest: date) -> bool:
    """Whether this chat has a conversation file dated on or after `oldest`.

    The legacy monolithic `conversations.jsonl` carries no date in its name, so
    a chat that still has one is always considered — it is the shape `tail`
    falls back to, and guessing its age from an mtime would drop a real
    conversation to save a single read.
    """
    day_dir = log_dir("channels") / channel / chat_id / "conversations"
    if not day_dir.is_dir():
        return True
    try:
        stems = [p.stem for p in day_dir.glob("*.jsonl")]
    except OSError:
        return True
    if not stems:
        return True
    for stem in stems:
        try:
            if date.fromisoformat(stem) >= oldest:
                return True
        except ValueError:
            return True  # an unparseable name is not evidence of age
    return False


COLLECTORS: tuple[Collector, ...] = (mirror_chats, channel_chats)


def idle_conversations(
    conversations: Iterable[Conversation],
    *,
    now: datetime,
    idle_minutes: int,
    lookback_hours: int,
) -> list[Conversation]:
    """The ones that have gone quiet recently enough to still be about today.

    Two bounds, and the second is not a tidiness rule: without it the first
    pass on a machine with history would recap every conversation it has ever
    held, all at once. A conversation quiet for longer than the lookback did
    not just end — it ended a while ago, and whatever was worth keeping from it
    was kept by whatever was running then.
    """
    quiet_by = now - timedelta(minutes=idle_minutes)
    oldest = now - timedelta(hours=lookback_hours)
    return [
        conv
        for conv in conversations
        if oldest <= conv.last_turn_at <= quiet_by
    ]


__all__ = [
    "COLLECTORS",
    "Collector",
    "Conversation",
    "MIRROR_SOURCE",
    "Turn",
    "channel_chats",
    "idle_conversations",
    "mirror_chats",
]
