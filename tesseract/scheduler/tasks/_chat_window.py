"""Which conversations a nightly job reads, and which day a turn belongs to.

Two jobs summarise the previous day — the chat digest and the feedback sweep —
and both were carrying their own copy of this, differing only in a type
annotation. One copy, because the question is one question.

**A day here is a UTC calendar day, and that is not the same day the record
store means.** `chat_store` decides staleness by the operator's LOCAL date and
the drawer groups by it, so away from UTC the two disagree at the edges — a
late-evening turn is filed under a day the operator has not reached yet. The
split is real and is left alone here deliberately: closing it moves content
between the `memory-store/daily/<date>.md` notes someone already has, which is
their call to make rather than a refactor's.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from tesseract.mirror.server import chat_store
from tesseract.mirror.server.chat_store import ChatRecord


def parse_stamp(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_day(stamp: str | None) -> date | None:
    """The UTC calendar date a stamp falls on, or None if it will not parse."""
    parsed = parse_stamp(stamp)
    return parsed.astimezone(timezone.utc).date() if parsed is not None else None


def message_day(msg: dict[str, Any]) -> date | None:
    """The UTC date one turn was said on. None when the turn is unstamped —
    history predating per-message timestamps, which callers treat as "cannot
    place" rather than "does not count"."""
    stamp = msg.get("timestamp")
    return utc_day(stamp) if isinstance(stamp, str) and stamp.strip() else None


def target_day(fired_at: datetime) -> date:
    """The day these jobs summarise: the UTC day before the one they ran in."""
    return (fired_at - timedelta(days=1)).date()


def records_covering(target: date) -> list[ChatRecord]:
    """Every chat record whose wall-clock span covers `target`.

    ARCHIVED ONES INCLUDED, deliberately: these jobs run the morning after the
    day they read, and the first connection of a new day archives every chat
    left open on the previous one — so filtering them out would find an empty
    tree exactly when there is the most to say. Archiving is a shelf, not a
    retraction.

    A record is kept when `target` falls within `[start.date(), end.date()]`
    rather than when a single stamp matches. A conversation that crossed
    midnight used to land entirely in the later day's digest and go missing
    from the earlier one's. It now appears in both, and the callers filter to
    the target day per MESSAGE — which is what keeps a record that spans a week
    from being reported as one day's work.
    """
    kept: list[ChatRecord] = []
    for record in chat_store.list_records(include_archived=True):
        start = parse_stamp(record.started_at)
        if start is None:
            continue
        end = parse_stamp(record.ended_at) or start
        if start.astimezone(timezone.utc).date() <= target <= end.astimezone(timezone.utc).date():
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
        if msg.get("_reasoning"):
            continue
        if msg.get("role") not in ("user", "assistant"):
            continue
        day = message_day(msg)
        if day is not None and day != target:
            continue
        if day is None and stamped:
            continue
        out.append(msg)
    return out
