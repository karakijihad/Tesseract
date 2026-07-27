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

from typing import Any

from tesseract.orchestrator.background_event_bus import get_background_bus
from tesseract.orchestrator.activity.models import ActivityRecord, ActivityRecordOut

CHANNEL = "activity"


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


def publish_activity_event(*, kind: str, record: ActivityRecord) -> None:
    """Publish an activity event to the process-wide background bus.
    Non-blocking; safe when no operator is connected (drains to the ring
    buffer, though the Mirror activity pump hydrates via REST, not replay)."""
    get_background_bus().publish(kind, make_activity_envelope(kind=kind, record=record))


__all__ = ["CHANNEL", "make_activity_envelope", "publish_activity_event"]
