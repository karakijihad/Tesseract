"""One reading of a timestamp for the panel's routes.

Every autonomy feed answers the same two questions about a time: turn what the
runtime holds into UTC ISO for the wire, and turn what a record wrote back into
an aware datetime. Four routes had written that pair themselves, byte for byte,
and a fifth was about to.

Naive in means UTC, deliberately and in one place. The scheduler's own log
notes that its callers disagree about what a naive stamp means; on this panel
they do not, because everything reaching a surface has already been normalised
by whoever wrote the record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def iso(value: datetime | None) -> str | None:
    """UTC ISO, or None. A naive datetime is read as UTC."""
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


def parse(value: Any) -> datetime | None:
    """An aware datetime from whatever a record wrote, or None.

    Never raises: a stamp this cannot read is a fact the panel does not have,
    which is the honest answer and not a 500 on a dashboard.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def took(duration_ms: Any) -> str:
    """How long something ran, in words. Empty when nothing measured it.

    Here rather than on one room because two of them ask it: Overview's
    schedule rows, and the steps under a run on an entry card. The panel's
    rule is that the words are the backend's, and two backends writing them
    is how one night comes to be described two ways.
    """
    try:
        seconds = float(duration_ms) / 1000
    except (TypeError, ValueError):
        return ""
    if seconds < 1:
        # `0.0s` reads as a rendering fault rather than as a fast run.
        return f"{round(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


__all__ = ["iso", "parse", "took"]
