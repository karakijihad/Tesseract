"""The plumbing every card-filing job shares: where the queue is, and telling
an open cockpit about a card that just landed.

Three jobs file proposal cards now. These calls were written twice before the
third arrived, which is the point at which two copies stop being a coincidence:
the second copy of `_broadcast` differed from the first only in the name it
logged under, and the second `logs_dir` carried a resolved-then-discarded local
the first had already dropped.

The one-at-a-time question used to live here too. It moved to
`EventStore.one_is_pending` when a TOOL began filing cards as well, and the
forwarding wrapper left behind was deleted once it had one caller: a second
name for one operation is the thing this module exists to prevent.

Nothing here decides anything about a card. What a job proposes, when it may
propose it, and what its evidence is stay with the job; this is only how a
card reaches the queue and the screen.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from tesseract.paths import home_logs_root
from tesseract.scheduler.types import JobContext

log = logging.getLogger(__name__)


def logs_dir(ctx: JobContext) -> Path:
    """The home log tree, or whatever the row overrode it with.

    `home_logs_root()` resolves `TESSERACT_HOME` at call time, which is the
    whole of the env handling.
    """
    override = (ctx.config or {}).get("logs_dir")
    return Path(override) if override else home_logs_root()


def store(ctx: JobContext) -> Any:
    from tesseract.workspace_events import EventStore

    return EventStore(logs_dir(ctx))


async def announce(ctx: JobContext, event: Any, *, who: str) -> None:
    """Push the card to any open cockpit, best effort. A card that only
    appears on the next reload is still a card."""
    try:
        from tesseract.workspace_events.broadcast import broadcast_workspace_event

        if ctx.app is not None:
            await broadcast_workspace_event(ctx.app, event)
    except Exception:
        log.warning("%s: broadcast failed", who, exc_info=True)


__all__ = ["announce", "logs_dir", "store"]
