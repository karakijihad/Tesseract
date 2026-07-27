"""``AutonomyEventBus`` — buffers, fanout, dedupe via event_id."""

from __future__ import annotations

import pytest

from tesseract.orchestrator.autonomy import AutonomyEvent, AutonomyEventBus
from tesseract.orchestrator.autonomy.models import AgendaSource


@pytest.mark.asyncio
async def test_publish_appends_to_source_buffer() -> None:
    bus = AutonomyEventBus()
    await bus.publish(AutonomyEvent.make(AgendaSource.SELF_REFLECTION, {"observation": "x"}))
    buffered = bus.peek(AgendaSource.SELF_REFLECTION)
    assert len(buffered) == 1
    assert buffered[0].payload == {"observation": "x"}


@pytest.mark.asyncio
async def test_drain_clears_buffer() -> None:
    bus = AutonomyEventBus()
    await bus.publish(AutonomyEvent.make(AgendaSource.PROVIDER_WATCH, {"ok": False}))
    drained = bus.drain()
    assert len(drained) == 1
    assert bus.peek(AgendaSource.PROVIDER_WATCH) == []


@pytest.mark.asyncio
async def test_handler_invoked_and_failures_isolated() -> None:
    bus = AutonomyEventBus()
    seen: list[AutonomyEvent] = []

    async def good(event: AutonomyEvent) -> None:
        seen.append(event)

    async def bad(event: AutonomyEvent) -> None:
        raise RuntimeError("intentional")

    bus.subscribe(AgendaSource.OPERATOR, good)
    bus.subscribe(AgendaSource.OPERATOR, bad)
    await bus.publish(AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "test"}))
    # Both handlers fire; bad raising does not eat the good one.
    assert len(seen) == 1
    # Buffer still has the event so the kernel can ingest it on its tick.
    assert len(bus.peek(AgendaSource.OPERATOR)) == 1


def test_publish_nowait_buffers_without_handlers() -> None:
    bus = AutonomyEventBus()
    bus.publish_nowait(AutonomyEvent.make(AgendaSource.VAULT_SIGNAL, {"test": "t"}))
    assert len(bus.peek(AgendaSource.VAULT_SIGNAL)) == 1


@pytest.mark.asyncio
async def test_drain_by_source_filters() -> None:
    bus = AutonomyEventBus()
    await bus.publish(AutonomyEvent.make(AgendaSource.SELF_REFLECTION, {}))
    await bus.publish(AutonomyEvent.make(AgendaSource.PROVIDER_WATCH, {"ok": False}))
    out = bus.drain(AgendaSource.SELF_REFLECTION)
    assert len(out) == 1 and out[0].source == AgendaSource.SELF_REFLECTION
    # Provider-watch still pending.
    assert len(bus.peek(AgendaSource.PROVIDER_WATCH)) == 1


@pytest.mark.asyncio
async def test_unsubscribe_drops_handler() -> None:
    bus = AutonomyEventBus()
    seen: list[AutonomyEvent] = []

    async def collector(event: AutonomyEvent) -> None:
        seen.append(event)

    token = bus.subscribe(AgendaSource.OPERATOR, collector)
    bus.unsubscribe(token)
    await bus.publish(AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "x"}))
    assert seen == []


def test_buffer_ring_caps_at_size() -> None:
    bus = AutonomyEventBus(buffer_size=3)
    for i in range(5):
        bus.publish_nowait(
            AutonomyEvent.make(AgendaSource.SELF_REFLECTION, {"i": i})
        )
    drained = bus.drain(AgendaSource.SELF_REFLECTION)
    # Oldest two were evicted; deque holds the most recent three.
    assert [e.payload["i"] for e in drained] == [2, 3, 4]
