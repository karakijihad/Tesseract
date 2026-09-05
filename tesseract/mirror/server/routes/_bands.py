"""One producer's result, or what the room says when it could not be read.

Every Autonomy room is the same shape: three or four producers gathered with
`return_exceptions=True`, because one of them failing must cost that band and
not the room. Each room had its own copy of the unwrap, five identical lines
four times over, and four copies of an error path is four chances for one of
them to start swallowing quietly.

The rule they all follow, and the reason the fallback is the caller's: a band
that could not be read is a band with nothing in it, and what nothing looks
like belongs to the room. The log line names the room, so a warning in the
backend log still says which surface went thin.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


def band_for(room: str) -> Callable[[Any, Any], Any]:
    """The unwrap this room's handler uses, with its own name already in it."""

    def band(value: Any, fallback: Any) -> Any:
        """`value`, unless gathering it raised, in which case `fallback`."""
        if isinstance(value, BaseException):
            log.warning("%s route: a producer could not be read", room, exc_info=value)
            return fallback
        return value

    return band


__all__ = ["band_for"]
