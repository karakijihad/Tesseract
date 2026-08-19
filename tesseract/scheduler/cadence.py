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


__all__ = ["INTERVAL_RE", "next_fire", "parse_interval"]
