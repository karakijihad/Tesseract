"""Natural-language time + recurrence parsing for alarms.

Shared by the Mirror WS command handlers and the kernel alarm tools — lives
under `scheduler/` to avoid a mirror↔kernel import cycle.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

from tesseract.scheduler.alarms import RecurrenceRule

ALARM_HANDLER_DOTPATH = "tesseract.scheduler.tasks.alarm_handler.AlarmHandlerJob"

_RELATIVE_DURATION_RE = re.compile(
    r"^\s*(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?\s*$"
)

_WEEKDAY_MAP = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_UNIT_SECONDS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
}


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", text.strip()) if t]


def _parse_clock(token: str) -> time | None:
    """Parse '9am', '9:30am', '14:00', '09:00', 'noon', 'midnight' → time. None on miss."""
    text = token.lower().strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)", text)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        suffix = m.group(3)
        if not (1 <= hh <= 12) or not (0 <= mm <= 59):
            return None
        if suffix == "am":
            hh = 0 if hh == 12 else hh
        else:  # pm
            hh = 12 if hh == 12 else hh + 12
        return time(hh, mm)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return time(hh, mm)
    if text == "noon":
        return time(12, 0)
    if text == "midnight":
        return time(0, 0)
    return None


def _parse_compact_duration(token: str) -> int | None:
    """'30s', '15m', '1h30m' → seconds. None on miss or zero."""
    m = _RELATIVE_DURATION_RE.match(token)
    if not m or not any(m.group(g) for g in ("h", "m", "s")):
        return None
    seconds = (
        int(m.group("h") or 0) * 3600
        + int(m.group("m") or 0) * 60
        + int(m.group("s") or 0)
    )
    return seconds if seconds > 0 else None


def _at_clock_today_or_tomorrow(clock: time, now: datetime) -> datetime:
    """`now` is treated as the local-time anchor — `.replace(hour=...)` keeps
    its tzinfo (or stays naive). Caller is responsible for UTC normalization.
    """
    candidate = now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _to_utc(dt: datetime) -> datetime:
    """Normalize naive (system-local) or tz-aware datetime to UTC tz-aware.

    Naive `.astimezone()` in py3.6+ treats the value as system local, so this
    is a no-op for already-UTC inputs and a local→UTC conversion for naive
    inputs.
    """
    return dt.astimezone(timezone.utc)


def _to_local(now_utc: datetime) -> datetime:
    """Convert a UTC tz-aware (or naive-treated-as-UTC) datetime to a tz-aware
    system-local datetime. Wall-clock parsers ("9am", "tomorrow at HH")
    operate on this so they anchor to the operator's wall clock, not UTC.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone()


def _try_iso(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_alarm_when(raw: str, now: datetime) -> datetime | None:
    """Return an absolute UTC datetime for `raw`, or None if unparseable.

    Wall-clock expressions ('9am', 'tomorrow at 9am', 'next mon at 14:00')
    are interpreted in **system local time** so the operator can say "9am"
    and mean their wall clock, not UTC. Relative durations and ISO 8601 are
    timezone-anchored already and unaffected. `now` may be UTC or local;
    it's normalized internally.

    Accepted forms:
      - compact relative: '30s', '15m', '1h', '1h30m', '2h15m30s'
      - spelled-out relative: 'in 20 minutes', 'in 2 hours'
      - clock-only: '9am', '14:00', '9:30pm' (today if future, else tomorrow)
      - 'tomorrow at 9am', 'tomorrow 9am'
      - 'next mon at 9am'
      - ISO 8601 (naive treated as UTC)
    """
    tokens = _tokenize(raw.lower())
    if not tokens:
        return None
    now_local = _to_local(now)
    run_at, consumed = _parse_time_tokens(tokens, now_local)
    if run_at is None or consumed != len(tokens):
        return None
    return _to_utc(run_at)


def _parse_time_tokens(tokens: list[str], now: datetime) -> tuple[datetime | None, int]:
    """Greedy time parse on the token sequence starting at index 0. Returns
    (datetime, tokens_consumed). (None, 0) on miss."""
    if not tokens:
        return (None, 0)
    head = tokens[0]

    seconds = _parse_compact_duration(head)
    if seconds is not None:
        return (now + timedelta(seconds=seconds), 1)

    iso = _try_iso(head)
    if iso is not None:
        return (iso, 1)

    if head == "in" and len(tokens) >= 3 and tokens[1].isdigit() and tokens[2] in _UNIT_SECONDS:
        n = int(tokens[1])
        if n <= 0:
            return (None, 0)
        return (now + timedelta(seconds=n * _UNIT_SECONDS[tokens[2]]), 3)

    if head == "tomorrow":
        idx = 1
        if idx < len(tokens) and tokens[idx] == "at":
            idx += 1
        if idx >= len(tokens):
            return (None, 0)
        clock = _parse_clock(tokens[idx])
        if clock is None:
            return (None, 0)
        base = (now + timedelta(days=1)).replace(
            hour=clock.hour, minute=clock.minute, second=0, microsecond=0
        )
        return (base, idx + 1)

    if head == "next" and len(tokens) >= 2 and tokens[1] in _WEEKDAY_MAP:
        target = _WEEKDAY_MAP[tokens[1]]
        idx = 2
        if idx < len(tokens) and tokens[idx] == "at":
            idx += 1
        if idx >= len(tokens):
            return (None, 0)
        clock = _parse_clock(tokens[idx])
        if clock is None:
            return (None, 0)
        days_ahead = (target - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        base = (now + timedelta(days=days_ahead)).replace(
            hour=clock.hour, minute=clock.minute, second=0, microsecond=0
        )
        return (base, idx + 1)

    clock = _parse_clock(head)
    if clock is not None:
        return (_at_clock_today_or_tomorrow(clock, now), 1)

    if head == "at" and len(tokens) >= 2:
        clock = _parse_clock(tokens[1])
        if clock is not None:
            return (_at_clock_today_or_tomorrow(clock, now), 2)

    return (None, 0)


def parse_recurrence(tokens: list[str]) -> tuple[RecurrenceRule | None, int]:
    """Return (rule, tokens_consumed). Accepted forms:

      - 'daily', 'every day'
      - 'weekdays', 'every weekday'
      - 'every <weekday>'              → weekly on that day
      - 'every <N><h|m>' or 'every <N> <unit>' → interval
    """
    if not tokens:
        return (None, 0)
    head = tokens[0]
    if head == "daily":
        return (RecurrenceRule(kind="daily"), 1)
    if head == "weekdays":
        return (RecurrenceRule(kind="weekdays"), 1)
    if head != "every" or len(tokens) < 2:
        return (None, 0)

    second = tokens[1]
    if second in ("day",):
        return (RecurrenceRule(kind="daily"), 2)
    if second in ("weekday", "weekdays"):
        return (RecurrenceRule(kind="weekdays"), 2)
    if second in _WEEKDAY_MAP:
        return (RecurrenceRule(kind="weekly", weekday=_WEEKDAY_MAP[second]), 2)

    seconds = _parse_compact_duration(second)
    if seconds is not None:
        return (RecurrenceRule(kind="every", interval_seconds=seconds), 2)

    if len(tokens) >= 3 and second.isdigit() and tokens[2] in _UNIT_SECONDS:
        n = int(second)
        if n > 0:
            return (RecurrenceRule(kind="every", interval_seconds=n * _UNIT_SECONDS[tokens[2]]), 3)

    return (None, 0)


def parse_alarm_spec(raw: str, now: datetime) -> tuple[datetime | None, RecurrenceRule | None, str]:
    """Combined parser for `<when> [message]` with optional recurrence prefix.

    Grammar (greedy left-to-right):
        [recurrence-phrase] [time-phrase] [message-tail]

    A recurrence alone (e.g. 'daily "stand up"') anchors the first fire to
    the next cycle-occurrence from now. Returns (None, recurrence, raw) if no
    time expression is present and recurrence lacks an implicit anchor.
    """
    tokens = _tokenize(raw.lower())
    if not tokens:
        return (None, None, "")

    recurrence, rec_used = parse_recurrence(tokens)
    idx = rec_used

    # Wall-clock parsing in local; recurrence anchor in local too so "every
    # day at 9am" lands on local 9am. Final UTC normalization at return.
    now_local = _to_local(now)

    run_at: datetime | None = None
    time_used = 0
    if idx < len(tokens):
        run_at, time_used = _parse_time_tokens(tokens[idx:], now_local)
        idx += time_used

    if run_at is None and recurrence is not None:
        run_at = recurrence.next_occurrence(now_local)

    if run_at is None:
        return (None, recurrence, raw)

    original_tokens = _tokenize(raw)
    message_tokens = original_tokens[idx:]
    message = " ".join(message_tokens).strip()
    if len(message) >= 2 and message[0] == message[-1] and message[0] in ('"', "'"):
        message = message[1:-1].strip()
    return (_to_utc(run_at), recurrence, message)
