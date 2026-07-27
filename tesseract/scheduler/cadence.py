"""Cadence parsing shared between ``SchedulerEngine.add_job_runtime`` and
higher-level loop-creation call sites.

Single source of truth for the interval-shorthand grammar
(``15m`` / ``2h30m`` / ``1d12h``). Cron expressions are still validated by
``croniter`` directly at the caller site.
"""

from __future__ import annotations

import re

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


__all__ = ["INTERVAL_RE", "parse_interval"]
