"""Telling an open panel that something it cached is no longer current.

A panel fetches once and keeps what it got (`lib/useCachedFetch.ts`), which is
right: switching between settings rows must not repaint a spinner over data
that has not changed. What was missing is the other half. When the backend
writes something a panel is showing, the panel had no way to hear about it,
and the operator's only move was to reload the window by hand.

That was measured: the assistant asked for three fields on an account and the
Credentials panel showed the same row it had been showing, with no boxes,
until it was reloaded.

**One envelope, naming the cache key.** Not a credentials envelope, and not a
copy of the panel's own fetch. `useCachedFetch` already keys every cached
thing by a string, so the smallest true message is that string: the hook
refetches whatever is holding it, and any panel is a caller without knowing
this module exists. Credentials is simply the first.

Publishing is best effort. A write that succeeded must not be undone because
nobody was listening, and the panel's next ordinary fetch reads the same state
off disk regardless.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

CHANNEL = "panel"

#: The one event on this channel. Named for what happened, not for what the
#: receiver should do: the backend knows a thing was written, and whether that
#: is worth a refetch is the panel's decision.
EVENT = "panel_cache_stale"

#: What `useCachedFetch` calls the credentials listing. Named here so the two
#: sides of one string are one edit apart rather than one grep apart.
CREDENTIALS_KEY = "settings.credentials"

#: The Autonomy panel's Thrown away room. It does not go through
#: `useCachedFetch` — the panel has its own store and its own poll — but the
#: stale registry those two share is `stores/stale.ts`, so a room watching a
#: key is a caller of this the same way a hook is. The room polls slowly on
#: purpose, and a window changed from the room or asked for on a phone is not
#: something to find out about a minute and a half later.
RETENTION_KEY = "autonomy.retention"

#: The Autonomy panel's Atlas room, for the same reason and with one
#: difference: what makes it stale is not a setting somebody typed but a step
#: somebody ran. So the mapping below is what a caller consults, rather than
#: every caller knowing which rooms care about which step.
ATLAS_KEY = "autonomy.atlas"

#: Which pipeline stage, run on its own, leaves which room reading the state
#: before it. Declared here rather than in the tool that runs a stage: the
#: tool's job is to run the step it was given, and it would otherwise have to
#: learn what a map is. A stage that is not in this table makes no room stale,
#: which is the honest default — a room that is not showing a stage's output
#: has nothing to hear about.
STAGE_KEYS: dict[str, str] = {
    "atlas_build": ATLAS_KEY,
    "atlas_verify": ATLAS_KEY,
}


def publish_stale_for_stage(stage: str) -> None:
    """Tell whatever room shows this stage's output that it is behind.

    Called after a stage that actually RAN. A refusal changes nothing on any
    surface, so nudging a room to re-read the same bytes would be a poll the
    operator did not ask for.
    """
    key = STAGE_KEYS.get(stage)
    if key is not None:
        publish_stale(key)


def publish_stale(key: str) -> None:
    """Say that ``key`` is out of date, to every open panel.

    Never raises. Call it from the event loop, which is where
    ``BackgroundEventBus.publish`` may be called from; a write that ran on a
    worker thread publishes after it is awaited.
    """
    try:
        from tesseract.orchestrator.background_event_bus import get_background_bus

        envelope: dict[str, Any] = {
            "kind": EVENT,
            "channel": CHANNEL,
            "session_id": "",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data": {"key": key},
        }
        get_background_bus().publish(EVENT, envelope)
    except Exception:  # noqa: BLE001 — a panel that missed a nudge refetches later
        log.warning("panel_refresh: could not announce %s as stale", key, exc_info=True)


__all__ = [
    "ATLAS_KEY",
    "CHANNEL",
    "CREDENTIALS_KEY",
    "EVENT",
    "RETENTION_KEY",
    "STAGE_KEYS",
    "publish_stale",
    "publish_stale_for_stage",
]
