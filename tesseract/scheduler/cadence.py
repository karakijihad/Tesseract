"""Cadence parsing shared between ``SchedulerEngine.add_job_runtime`` and
higher-level loop-creation call sites.

Single source of truth for the interval-shorthand grammar
(``15m`` / ``2h30m`` / ``1d12h``), and — since the watchman began judging
whether a row fired on time — for the one conversion that decides WHEN a
cadence next comes due. That conversion is easy to get subtly wrong and was
written twice: cron is read in system LOCAL time so an operator's ``0 23 * * *``
means their own 23:00, while every stored time is UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from croniter import croniter

INTERVAL_RE = re.compile(
    r"^\s*(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?\s*$"
)


def parse_interval(cadence: str) -> int | None:
    """Return interval in seconds for shorthand like '15m' / '2h30m' / '1d';
    ``None`` when ``cadence`` is not interval shorthand."""
    match = INTERVAL_RE.match(cadence)
    if not match or not any(match.group(g) for g in ("d", "h", "m", "s")):
        return None
    seconds = (
        int(match.group("d") or 0) * 86400
        + int(match.group("h") or 0) * 3600
        + int(match.group("m") or 0) * 60
        + int(match.group("s") or 0)
    )
    return seconds if seconds > 0 else None


def names_an_hour(cadence: str) -> bool:
    """Whether this cadence names an HOUR OF THE DAY rather than a rate.

    `0 23 * * *` is a wall clock: somebody picked 23:00 on one machine, and on
    another machine 23:00 is a different moment in the operator's evening. A
    rate has no opinion about what time it is where they live — `15 * * * *`
    fires every hour, `*/5 * * * *` every five minutes and `24h` every day of
    uptime, and moving any of those is a change to how OFTEN work happens.

    The one predicate, because two things ask it: the check on which rows ship
    a wall clock, and the rule deciding which of a shipped row's fields the
    operator owns. Two copies of it would drift into disagreeing about which
    rows those are. It answers a question about ONE cadence and has never
    counted them, which is why adding `morning` beside `consolidate` cost this
    function nothing and cost the sentences around it several corrections.
    """
    fields = cadence.split()
    return len(fields) == 5 and fields[1] != "*"


def moves_the_hour(shipped: str, wanted: str) -> bool:
    """Whether `wanted` is `shipped` moved to another time of day, and nothing
    else.

    Both have to name an hour, and the three fields after the hour have to be
    identical: `0 23 * * *` to `30 7 * * *` moves the same daily pass to the
    morning, while `0 23 * * 1` turns a nightly job into a weekly one, which is
    a different question from what time it is.
    """
    if not (names_an_hour(shipped) and names_an_hour(wanted)):
        return False
    return shipped.split()[2:] == wanted.split()[2:]


def a_tick_fell_between(cadence: str, earlier: datetime, now: datetime) -> bool:
    """Whether a cron point sits in `(earlier, now]`. Both LOCAL and naive.

    The predicate for a tick the loop never saw. `SchedulerEngine._should_fire`
    matches a cron by the MINUTE, so a scheduled moment that passed while the
    process was suspended (the machine slept, the loop stalled, a trigger pass
    ran long) is never matched and the row is simply skipped until tomorrow.
    The boot catch-up does not cover it either, because nothing rebooted.

    Asked of the GAP between two consecutive ticks rather than of the row's
    last fire, and the difference matters: a row that was disabled for a month
    and switched back on at three in the afternoon has not missed a tick, it
    was off, and a rule reading its last fire would start it at once.

    Exclusive at the earlier end and inclusive at `now`, so a point exactly on
    the previous tick is not re-served and one landing on this tick is caught
    whether or not the minute also matches.

    Intervals answer False and are not a gap in this sense: they are measured
    from the last fire, so a suspended process resumes overdue and fires on its
    own rule the moment it is asked.
    """
    if parse_interval(cadence):
        return False
    try:
        return croniter(cadence, earlier).get_next(datetime) <= now
    except Exception:  # noqa: BLE001 - an unreadable cadence misses nothing
        return False


def next_fire(cadence: str, after: datetime) -> datetime | None:
    """When `cadence` next comes due after `after`. UTC in, UTC out.

    ``None`` when nothing can read the cadence — the caller decides whether
    that is an error to raise or a row to skip.

    The local step is the whole point: `croniter` is given a NAIVE local
    datetime, and a naive `.astimezone()` is read as system local on the way
    back, which is what makes `0 23 * * *` mean the operator's 23:00 on any
    host. `SchedulerEngine._should_fire` matches a cron against local time for
    the same reason.
    """
    seconds = parse_interval(cadence)
    if seconds:
        return after + timedelta(seconds=seconds)
    try:
        local = after.astimezone().replace(tzinfo=None)
        return croniter(cadence, local).get_next(datetime).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001 — an unreadable cadence is the caller's to report
        return None


def due_after_last(
    cadence: str, last_fired_at: datetime | None, now: datetime
) -> datetime | None:
    """When a ROW next comes due, which is a different question from `next_fire`.

    An interval is measured from the last fire, not from the clock:
    `SchedulerEngine._should_fire` fires when `now - last_fired_at >= interval`.
    So a five minute row last fired at 12:00 is due at 12:05 whatever time it
    is asked, and answering `now + 5m` put a future time on a row the engine
    already considered overdue. The answer can be in the past, and that is the
    honest one: it means the row is due and has not gone yet.

    A cron cadence names absolute points, so the last fire tells it nothing and
    it is answered from `now`. `next_fire` keeps its own meaning, which
    `watchman/rows.py` depends on to walk from one fire to the next.
    """
    if parse_interval(cadence) and last_fired_at is not None:
        return next_fire(cadence, last_fired_at)
    return next_fire(cadence, now)


@dataclass(frozen=True)
class Reading:
    """Everything a surface needs to know about a cadence, from one reader.

    `ok` is whether the scheduler will take it, `problem` says what is wrong in
    the words the person typing it needs, `words` is how it reads, and
    `next_fire_at` is when it comes round. A surface renders these; it does not
    work any of them out.
    """

    ok: bool
    problem: str
    words: str
    next_fire_at: datetime | None


def explain(
    cadence: str, now: datetime, last_fired_at: datetime | None = None
) -> Reading:
    """What this cadence means, and it is the only place that decides.

    The form that creates a job used to answer this itself, in a hand-written
    parser beside a hand-written matcher, against a scheduler that reads the
    same strings with croniter. The two disagreed four separate times: named
    weekdays and named months were refused, Sunday's second number was refused,
    day-of-month was required WITH day-of-week where cron wants either, and
    `?`, `L` and `#` were refused. Each was a cadence the operator could not
    create and the runtime would have run happily.

    So there is one reader now and every surface asks it. A second one is not a
    second opinion, it is the next four defects.
    """
    text = (cadence or "").strip()
    if not text:
        return Reading(False, "Nothing was typed, so nothing says when it runs.", "", None)
    if parse_interval(text) is None and INTERVAL_RE.match(text):
        return Reading(
            False,
            "A gap has to be more than zero, or it would run without stopping.",
            "",
            None,
        )
    if parse_interval(text) is None:
        try:
            croniter(text, now.astimezone().replace(tzinfo=None))
        except Exception:  # noqa: BLE001
            # Not croniter's own words. It answers a bad expression with
            # "Exactly 5, 6 or 7 columns has to be specified for iterator
            # expression", which names its internals and tells the person
            # typing nothing they can act on.
            return Reading(
                False,
                "The scheduler cannot read this. It takes a gap like 15m or "
                "2h30m, or five cron fields like 0 8 * * MON-FRI.",
                "",
                None,
            )
    return Reading(True, "", in_words(text), due_after_last(text, last_fired_at, now))

# Written out rather than read from `strftime`, which answers in the host's
# LC_TIME. Every other word this module puts on a row is an English literal, so
# a translated day name beside them would be the one thing on the panel whose
# language depends on the machine.
_DAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def when_words(due: datetime, now: datetime) -> str:
    """A time a reader can act on, which a bare clock time is not.

    `next at 08:00` on a weekday row read on a Friday evening means Monday, and
    nothing on the row said so. Local time on both sides, because the day that
    matters is the operator's.
    """
    local_due, local_now = due.astimezone(), now.astimezone()
    at = f"{local_due.hour:02d}:{local_due.minute:02d}"
    days = (local_due.date() - local_now.date()).days
    if days <= 0:
        return f"at {at}"
    if days == 1:
        return f"tomorrow at {at}"
    if days < 7:
        return f"{_DAY_NAMES[local_due.weekday()]} at {at}"
    return f"{local_due.day} {_MONTH_NAMES[local_due.month - 1]} at {at}"


# Cron fields, in the order croniter reads them.
_CRON_FIELDS = 5

# Largest first, so `2h30m` reads as hours and minutes rather than as 150 of
# something.
_UNITS: tuple[tuple[int, str, str], ...] = (
    (86400, "day", "days"),
    (3600, "hour", "hours"),
    (60, "minute", "minutes"),
    (1, "second", "seconds"),
)

# Every weekday, in the two spellings croniter accepts.
_WEEKDAYS = frozenset({"1-5", "MON-FRI"})


def _span(seconds: int) -> str:
    """`9000` as `2 hours 30 minutes`."""
    said: list[str] = []
    left = seconds
    for size, one, many in _UNITS:
        count, left = divmod(left, size)
        if count:
            said.append(f"{count} {one if count == 1 else many}")
    return " ".join(said)


def in_words(cadence: str) -> str:
    """What this cadence says, in the words a person uses.

    Beside the grammar that parses it, for the reason `liveness.py::LABELS`
    and `manifest/entry.py::words_for` sit beside their own values: a surface
    that translated `*/5 * * * *` itself would be a second reading of the
    field, and the first one to disagree with the scheduler.

    An expression this cannot phrase is returned as it was written. A cadence
    shown raw is honest; a cadence shown wrongly is not.
    """
    seconds = parse_interval(cadence)
    if seconds:
        for size, one, _many in _UNITS:
            if seconds == size:
                return f"every {one}"
        return f"every {_span(seconds)}"
    parts = cadence.split()
    if len(parts) != _CRON_FIELDS:
        return cadence
    minute, hour, day, month, weekday = parts
    if (day, month) != ("*", "*"):
        return cadence
    if minute.startswith("*/") and minute[2:].isdigit() and hour == "*":
        every = int(minute[2:])
        return "every minute" if every == 1 else f"every {every} minutes"
    if minute.isdigit() and hour == "*":
        past = int(minute)
        return "hourly, on the hour" if past == 0 else f"hourly, {past} minutes past"
    if minute.isdigit() and hour.isdigit():
        at = f"{int(hour):02d}:{int(minute):02d}"
        if weekday == "*":
            return f"daily at {at}"
        if weekday in _WEEKDAYS:
            return f"weekdays at {at}"
    return cadence


__all__ = [
    "INTERVAL_RE",
    "Reading",
    "a_tick_fell_between",
    "due_after_last",
    "explain",
    "in_words",
    "moves_the_hour",
    "names_an_hour",
    "next_fire",
    "parse_interval",
    "when_words",
]
