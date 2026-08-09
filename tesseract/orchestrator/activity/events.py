"""AS-1 — activity event channel: the ``activity`` namespace on the
background bus.

Mirrors ``orchestrator/surfaces/events.py`` — one event substrate, filtered
at the Mirror WS pump (``mirror/server/ws.py``), not at the publisher. The
frontend re-keys ``channel == "activity"`` envelopes into its activity store.

Thread-safety: ``BackgroundEventBus.publish`` is loop-thread-only (see its
docstring). Callers that mutate the registry from a worker thread MUST hop
to the main loop first (``loop.call_soon_threadsafe``) — see AS-1 integration.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from tesseract.orchestrator.background_event_bus import get_background_bus
from tesseract.orchestrator.activity.models import ActivityRecord, ActivityRecordOut

log = logging.getLogger(__name__)

CHANNEL = "activity"

ActivityListener = Callable[[dict[str, Any]], None]

# Direct fan-out, alongside the bus rather than through it. The bus drops on a
# full subscriber queue and says so only in a log line, which is fine for the
# Mirror WS pump (it hydrates over REST) and wrong for a subscription whose
# whole contract is "you were told, or you were told you missed it". A listener
# here is called synchronously by the publisher and owns its own bounding.
_listeners: list[ActivityListener] = []


def make_activity_envelope(*, kind: str, record: ActivityRecord) -> dict[str, Any]:
    """Build the canonical activity event envelope (shape mirrors the surface
    channel). ``session_id`` carries the ``activity_id`` so the
    frontend can attribute the event without a separate field."""
    return {
        "kind": kind,
        "channel": CHANNEL,
        "session_id": record.activity_id,
        "ts": record.updated_at or record.started_at,
        "data": ActivityRecordOut.from_record(record).model_dump(),
    }


def subscribe_activity(listener: ActivityListener) -> None:
    """Register a direct listener. Idempotent — re-registering the same
    callable does not double-deliver."""
    if listener not in _listeners:
        _listeners.append(listener)


def unsubscribe_activity(listener: ActivityListener) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


def publish_activity_event(*, kind: str, record: ActivityRecord) -> None:
    """Publish an activity event to the process-wide background bus and to any
    direct listener. Non-blocking; safe when no operator is connected (drains
    to the ring buffer, though the Mirror activity pump hydrates via REST, not
    replay).

    A listener that raises must not stop the registry mutation that published
    the event, nor the listeners after it."""
    envelope = make_activity_envelope(kind=kind, record=record)
    get_background_bus().publish(kind, envelope)
    for listener in list(_listeners):
        try:
            listener(envelope)
        except Exception:  # noqa: BLE001
            log.exception("activity listener failed for %s", kind)


__all__ = [
    "CHANNEL",
    "ActivityListener",
    "make_activity_envelope",
    "publish_activity_event",
    "subscribe_activity",
    "unsubscribe_activity",
]
