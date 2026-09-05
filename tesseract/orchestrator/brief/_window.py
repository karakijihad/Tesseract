"""The day a brief is about, and how a record's timestamp is read.

Three pre-fetchers stand in front of the daily brief's grounded sections, and
each one asks the same two questions of every record it reads: is this inside
the day, and what moment does its timestamp mean. Each had its own copy of both
answers, byte for byte, so a correction to either had to be found three times
and nothing made the third copy follow.

**Midnight in the OPERATOR's zone, not UTC.** A brief dated D covers the day a
person would call D. Anchoring at UTC midnight made the window run 02:00 to
02:00 for an operator at UTC+02: two hours of the previous day counted as this
one, and the last two hours of the real day missing.

**Bounded on both sides.** The upper bound is what lets a past date be
re-rendered: without it, a record that changed after that date's window would
leak into a stale day's digest.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

DEFAULT_SINCE_HOURS = 24


def day_window(
    target_date: date, since_hours: int = DEFAULT_SINCE_HOURS,
) -> tuple[datetime, datetime]:
    """The window a brief dated `target_date` is about, as (cutoff, anchor)."""
    anchor = datetime.combine(target_date, datetime.min.time()).astimezone()
    return anchor - timedelta(hours=since_hours), anchor


def parse_iso(value: Any) -> datetime | None:
    """A record's timestamp as an aware UTC moment, or None if it is not one."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def in_window(
    moment: datetime | None, cutoff: datetime, anchor: datetime,
) -> datetime | None:
    """`moment` when it falls inside the window, otherwise None."""
    if moment is None or moment < cutoff or moment > anchor:
        return None
    return moment


__all__ = ["DEFAULT_SINCE_HOURS", "day_window", "in_window", "parse_iso"]
