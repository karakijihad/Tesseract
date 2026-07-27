"""Surface event channel — the ``surface`` namespace on the background bus.

Per ``phase-Y-2-surface-protocol.md §5`` the default (vs. a standalone
``surface_event_bus``) is to extend the existing ``BackgroundEventBus`` with
a namespaced channel, keeping one event substrate. Envelope shape:
``{kind, channel, view, ts, data}``. The Mirror WS pump
(``mirror/server/ws_connection.py::_surface_events_pump``) forwards ``channel ==
"surface"`` to every connected operator; the frontend re-keys to the
standard ``{type, category: "canvas", …}`` Envelope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tesseract.orchestrator.background_event_bus import get_background_bus

CHANNEL = "surface"


def make_surface_envelope(
    *,
    kind: str,
    view: str,
    data: dict[str, Any],
    ts: datetime | None = None,
) -> dict[str, Any]:
    """Build the canonical surface event envelope."""
    return {
        "kind": kind,
        "channel": CHANNEL,
        # ``session_id`` carries the view so the frontend can attribute the
        # event without a separate field — surfaces are view-scoped.
        "session_id": view,
        "view": view,
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "data": dict(data),
    }


def publish_surface_event(*, kind: str, view: str, data: dict[str, Any]) -> None:
    """Publish a surface event to the process-wide background bus.

    Non-blocking. Safe when no operator is connected — events drain into the
    ring buffer for replay on the next subscription.
    """
    envelope = make_surface_envelope(kind=kind, view=view, data=data)
    get_background_bus().publish(kind, envelope)


__all__ = ["CHANNEL", "make_surface_envelope", "publish_surface_event"]
