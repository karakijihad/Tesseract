"""Publisher hooks — existing event sources → AutonomyEventBus.

The kernel's bus is process-local. Publishers (recovery, scheduler,
mission reflection, observer) call thin helpers in this module to
forward a relevant event into the kernel's bus without importing the
kernel directly. The helpers no-op when the kernel is not running —
the Mirror lifecycle wires them after :func:`_start_autonomy_kernel`
succeeds.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from tesseract.orchestrator.autonomy.event_bus import (
    AutonomyEvent,
    AutonomyEventBus,
)
from tesseract.orchestrator.autonomy.models import AgendaSource

log = logging.getLogger(__name__)


# Module-level registry. The Mirror lifecycle sets this once the
# kernel starts; tests build their own bus and call ``set_active_bus``
# directly without going through the lifecycle.
_active_bus: AutonomyEventBus | None = None


def set_active_bus(bus: AutonomyEventBus | None) -> None:
    """Register / clear the bus publishers should target.

    Passing ``None`` disables forwarding — used during shutdown so a
    publisher that fires mid-teardown doesn't append into a stopped
    kernel's buffers."""
    global _active_bus
    _active_bus = bus


def get_active_bus() -> AutonomyEventBus | None:
    return _active_bus


#: How many events each source has lost to a missing bus, this process.
#: Read by `/api/autonomy` surfaces and by tests; the warning below fires only
#: on the first drop per source, and this is what says how big the silence got.
_dropped: dict[str, int] = {}


def dropped_event_counts() -> dict[str, int]:
    """Events discarded per source because no bus was registered."""
    return dict(_dropped)


def publish_to_bus(
    source: AgendaSource,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
) -> None:
    """Sync publisher. Drops the event if no bus is registered — and says so.

    Six scheduler jobs reach the agenda only through here. Without a bus each
    of them still runs, still logs success, and its findings go nowhere: a
    scout that found three leads is indistinguishable from one that found
    none. Dropping is the correct behaviour — there is nowhere to put the
    event — but doing it quietly is not, so the first loss per source is a
    warning and the rest are counted.
    """
    bus = _active_bus
    if bus is None:
        seen = _dropped.get(source.value, 0)
        _dropped[source.value] = seen + 1
        if seen == 0:
            log.warning(
                "autonomy publisher: no bus registered — discarding %s events "
                "(the kernel is not running; this job's findings reach nothing)",
                source.value,
            )
        return
    event = AutonomyEvent.make(source, payload, event_id=event_id)
    try:
        bus.publish_nowait(event)
    except Exception:
        log.exception("autonomy publisher: publish_nowait raised (source=%s)", source.value)


def make_workspace_event_forwarder(
    source: AgendaSource,
    *,
    kind_filter: str | tuple[str, ...] | None = None,
) -> Callable[[Any], Awaitable[None]]:
    """Build a callback that takes a :class:`WorkspaceEvent` and
    forwards it to the kernel bus as ``source``. ``kind_filter`` keeps
    the callback narrow — only events with a matching ``kind`` are
    forwarded (most consumers want one kind).
    """
    allowed: set[str] | None
    if kind_filter is None:
        allowed = None
    elif isinstance(kind_filter, str):
        allowed = {kind_filter}
    else:
        allowed = set(kind_filter)

    async def _forwarder(event: Any) -> None:
        if allowed is not None and getattr(event, "kind", None) not in allowed:
            return
        payload = {
            "kind": getattr(event, "kind", None),
            "title": getattr(event, "title", None),
            "summary": getattr(event, "summary", None),
            **dict(getattr(event, "payload", {}) or {}),
        }
        publish_to_bus(source, payload, event_id=getattr(event, "event_id", None))

    return _forwarder


__all__ = [
    "dropped_event_counts",
    "get_active_bus",
    "make_workspace_event_forwarder",
    "publish_to_bus",
    "set_active_bus",
]
