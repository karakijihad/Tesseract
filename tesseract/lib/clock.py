"""Which calendar day a moment belongs to, for the person using this machine.

**Operator ruling, 2026-08-31: the whole app is timezone based.** One rule, in
one place, because it had been written down once and applied in one module.

The rule, in two halves, and both halves matter:

- **An instant stays UTC.** Every timestamp on disk, every id with a time in
  it, every comparison between two events. UTC is what makes two records from
  two subsystems comparable, and a local offset is comparable only if every
  reader remembers to convert.
- **A calendar day is the operator's.** "Today", "yesterday", the date a brief
  is filed under, the day a spend cap resets on, the date written into a line
  a person reads. These are questions about the day somebody was living in,
  and answering them from a UTC instant is wrong by one day for a chunk of
  every evening --- eight hours of it in +02:00, more further east.

The failure that made this a ruling: the chat digest summarises the local day
before the local day it fired in, and its own test computed that day by
subtracting one from a UTC instant at 23:50. The two agreed in UTC and
disagreed by a day in +02:00, so the suite passed in CI and had been failing
on the operator's own machine.

`test_a_day_is_the_operators_day.py` reads the source tree and fails on a new
calendar decision taken from a UTC clock, so the ruling holds without anybody
remembering it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

__all__ = [
    "local_day",
    "parse_stamp",
    "to_local",
    "today",
    "yesterday",
]


def parse_stamp(stamp: str | None) -> datetime | None:
    """An ISO stamp as a datetime, or None if it will not parse.

    Accepts the `Z` suffix, which `datetime.fromisoformat` did not take before
    3.11 and which plenty of records on this machine still carry.
    """
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_local(moment: datetime) -> datetime:
    """`moment` on this machine's clock.

    One function rather than an `.astimezone()` at each place that asks, so the
    clock a day is decided on can be stood somewhere else for a test. A
    boundary case written in UTC literals cannot tell this rule from the UTC
    one it replaced, because on a UTC host the two are the same computation.

    Deliberately the zero-argument `.astimezone()`, which resolves the zone at
    the STAMP's instant: a summer turn read back in winter still belongs to the
    day it was said on.

    A naive datetime is read as local, which is what a naive datetime on this
    machine means. Reading it as UTC would silently shift every one of them.
    """
    return moment.astimezone()


def local_day(stamp: str | None) -> date | None:
    """The local calendar date a stamp falls on, or None if it will not parse."""
    parsed = parse_stamp(stamp)
    return to_local(parsed).date() if parsed is not None else None


def today() -> date:
    """The date it is for the person at this machine.

    Not `datetime.now(timezone.utc).date()`, which is a different day for the
    last hours of every evening east of Greenwich and the first hours of every
    morning west of it.
    """
    return datetime.now().astimezone().date()


def yesterday() -> date:
    """The day before `today`, on the same clock."""
    return today() - timedelta(days=1)
