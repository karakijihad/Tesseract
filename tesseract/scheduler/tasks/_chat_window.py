"""Which conversations a nightly job reads, and which day a turn belongs to.

Two jobs summarise the previous day — the chat digest and the feedback sweep —
and both were carrying their own copy of this, differing only in a type
annotation. One copy, because the question is one question.

**A day here is the operator's day, on this machine's clock.** Instants stay
UTC on disk; only the question "which calendar day does this belong to" is
answered locally. It has to be: `chat_store` decides staleness by the local
date and the drawer groups by it, so a UTC answer here filed a late-evening
turn under a day the operator had not reached yet.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from tesseract.lib import clock
from tesseract.mirror.server import chat_store
from tesseract.mirror.server.chat_store import ChatRecord

# Through the MODULE, never `from ... import to_local`. The clock moved to
# `lib/clock.py` on the operator's 2026-08-31 ruling that the whole app is
# timezone based, and a bound name would have left this module with a second
# seam: a test standing the machine at UTC+5 patches one place and every app
# that reads a day follows. A `from` import here silently would not.
__all__ = ["message_day", "records_covering", "target_day", "turns_on"]


def message_day(msg: dict[str, Any]) -> date | None:
    """The local date one turn was said on. None when the turn is unstamped —
    history predating per-message timestamps, which callers treat as "cannot
    place" rather than "does not count"."""
    stamp = msg.get("timestamp")
    return clock.local_day(stamp) if isinstance(stamp, str) and stamp.strip() else None


def target_day(fired_at: datetime) -> date:
    """The day these jobs summarise: the local day before the one they ran in.

    `fired_at` is a UTC instant, so the local day it fell on is the one the
    operator was living in when the job ran, not the one the clock in London
    was showing.
    """
    return (clock.to_local(fired_at) - timedelta(days=1)).date()


def records_covering(target: date) -> list[ChatRecord]:
    """Every chat record whose wall-clock span covers `target`.

    ARCHIVED ONES INCLUDED, deliberately: these jobs run the morning after the
    day they read, and the first connection of a new day archives every chat
    left open on the previous one — so filtering them out would find an empty
    tree exactly when there is the most to say. Archiving is a shelf, not a
    retraction.

    A record is kept when `target` falls within `[start.date(), end.date()]`
    rather than when a single stamp matches. On a single stamp a conversation
    that crossed midnight lands entirely in the later day's digest and goes
    missing from the earlier one's. It appears in both, and the callers filter
    to the target day per MESSAGE — which is what keeps a record that spans a
    week from being reported as one day's work.

    **Both surfaces**, through `operator_records`: a conversation the operator
    had on their phone is a conversation they had, and reading only the
    cockpit was the one-store ruling half applied. Somebody else's approved
    chat is not in it, which is that same reader's other half.
    """
    # **Bounded, and the bound is provable rather than hopeful.** Without it
    # every daily run parses the install's entire history, growing with how
    # long the operator has owned the app rather than with what they said
    # yesterday. `save_chat` derives `ended_at` from the last message BEFORE
    # writing the file, so a record's mtime is never earlier than its
    # `ended_at`; and a record only survives the span test below when its
    # local end date is on or after `target`. Two days of UTC margin covers
    # every timezone offset that can sit between the two.
    floor = datetime.combine(
        target - timedelta(days=2), time.min, tzinfo=timezone.utc
    ).timestamp()
    kept: list[ChatRecord] = []
    for record in chat_store.operator_records(
        include_archived=True, touched_since=floor
    ):
        start = clock.parse_stamp(record.started_at)
        if start is None:
            continue
        end = clock.parse_stamp(record.ended_at) or start
        if clock.to_local(start).date() <= target <= clock.to_local(end).date():
            kept.append(record)
    return kept


def turns_on(record: ChatRecord, target: date) -> list[dict[str, Any]]:
    """The user/assistant turns in `record` that were said on `target`.

    Filtered per message, not per record, and that is load-bearing: a record is
    a conversation for its whole LIFE, where the file this replaced was one
    connection's snapshot. A chat left open for a week covers every day in it,
    so without this a job would hand the model the entire history each night
    stamped as one day's.

    A record with no stamped messages at all contributes all of them — an
    unstamped turn is old history, and losing it is worse than dating it
    loosely.
    """
    stamped = any(
        message_day(msg) is not None
        for msg in record.history
        if msg.get("role") in ("user", "assistant")
    )
    out: list[dict[str, Any]] = []
    for msg in record.history:
        if msg.get("role") not in ("user", "assistant"):
            continue
        day = message_day(msg)
        if day is not None and day != target:
            continue
        if day is None and stamped:
            continue
        out.append(msg)
    return out
