"""Kernel-internal pub/sub for AU-5.

Lightweight in-process bus that the AutonomyKernel reads on every tick.
Distinct from the existing ``BackgroundEventBus``: subscribers register
per *source* (an ``AgendaSource`` value), handlers are async callables,
and a bounded per-source replay buffer lets the kernel ingest events
that arrived between ticks without losing ordering.

The bus is deliberately small. It is NOT the workspace event store and
not a process-wide singleton — the kernel owns its bus instance so test
fixtures construct fresh buses without touching shared state. Bridges
from ``BackgroundEventBus`` (scheduler completions, mission events) are
wired in :mod:`tesseract.orchestrator.autonomy.kernel`.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from tesseract.orchestrator.autonomy.models import AgendaSource

log = logging.getLogger(__name__)


DEFAULT_REPLAY_BUFFER = 64


@dataclass(frozen=True)
class AutonomyEvent:
    """A single event the kernel's mappers consume.

    ``event_id`` is the de-dupe anchor — mappers fold this into the
    ``source_event_id`` of the AgendaItem they emit so the same event
    replayed across ticks does not create duplicates.
    """

    source: AgendaSource
    event_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def make(
        cls,
        source: AgendaSource,
        payload: dict[str, Any] | None = None,
        *,
        event_id: str | None = None,
    ) -> "AutonomyEvent":
        return cls(
            source=source,
            event_id=event_id or f"evt_{uuid.uuid4().hex[:12]}",
            payload=dict(payload or {}),
        )


Handler = Callable[[AutonomyEvent], Awaitable[None]]


@dataclass
class SubscriptionToken:
    source: AgendaSource
    token: str


class AutonomyEventBus:
    """In-process pub/sub keyed by ``AgendaSource``.

    Concurrency model: single asyncio loop. ``publish`` is async and
    awaits every registered handler in turn (handlers are expected to
    be cheap — bridges forward into the kernel's per-source buffer
    instead of doing real work). ``drain`` returns the buffered events
    for the kernel's per-tick read.
    """

    def __init__(self, *, buffer_size: int = DEFAULT_REPLAY_BUFFER) -> None:
        self._handlers: dict[AgendaSource, dict[str, Handler]] = defaultdict(dict)
        self._buffers: dict[AgendaSource, deque[AutonomyEvent]] = defaultdict(
            lambda: deque(maxlen=buffer_size)
        )
        self._buffer_size = buffer_size
        self._wake: Callable[[], None] | None = None

    def set_wake(self, wake: Callable[[], None] | None) -> None:
        """Register what to call when an event lands, or `None` to clear.

        **The wake belongs here rather than to each publisher.** An event
        arriving IS the thing that should wake the kernel, and this is the one
        place every event passes through — so a new publisher wakes it by
        construction instead of by remembering to. Before this, `poke()`
        existed and nothing outside the kernel ever called it: the tick
        interval was not a fallback, it was the only wake there was.
        """
        self._wake = wake

    def _woken(self) -> None:
        """Never raises into a publisher. A wake that fails costs the fallback
        interval; a wake that propagates costs the caller's own work."""
        wake = self._wake
        if wake is None:
            return
        try:
            wake()
        except Exception:
            log.exception("autonomy bus: wake raised")

    def subscribe(self, source: AgendaSource, handler: Handler) -> SubscriptionToken:
        token = uuid.uuid4().hex[:12]
        self._handlers[source][token] = handler
        return SubscriptionToken(source=source, token=token)

    def unsubscribe(self, sub: SubscriptionToken) -> None:
        self._handlers.get(sub.source, {}).pop(sub.token, None)

    async def publish(self, event: AutonomyEvent) -> None:
        """Append to the source buffer and fan out to handlers.

        Buffer write happens *first* so even if every handler raises
        the kernel still drains the event on the next tick. Handler
        exceptions are logged and swallowed — one broken bridge cannot
        starve the kernel."""
        self._buffers[event.source].append(event)
        self._woken()
        for handler in list(self._handlers.get(event.source, {}).values()):
            try:
                await handler(event)
            except Exception:
                log.exception(
                    "autonomy bus: handler for %s raised", event.source.value
                )

    def publish_nowait(self, event: AutonomyEvent) -> None:
        """Buffer-only publish for sync callers (recovery scan, scheduler
        job result). The kernel drains via :meth:`drain` on its next
        tick; sync publishers should NEVER block on handlers."""
        self._buffers[event.source].append(event)
        self._woken()

    def drain(self, source: AgendaSource | None = None) -> list[AutonomyEvent]:
        """Pop every buffered event (FIFO). Pass ``source`` to drain a
        single bucket. Returns an empty list if nothing is queued."""
        if source is not None:
            events = list(self._buffers.get(source, ()))
            self._buffers[source].clear()
            return events
        events: list[AutonomyEvent] = []
        for src in list(self._buffers.keys()):
            events.extend(self._buffers[src])
            self._buffers[src].clear()
        return events

    def peek(self, source: AgendaSource) -> list[AutonomyEvent]:
        """Return a *copy* of the source buffer without consuming it.
        Useful in tests that assert what reached the bus."""
        return list(self._buffers.get(source, ()))

    def list_sources(self) -> list[str]:
        """Sources with at least one subscriber or queued event."""
        keys = set(self._handlers.keys()) | set(self._buffers.keys())
        return sorted(s.value for s in keys)


__all__ = [
    "AutonomyEvent",
    "AutonomyEventBus",
    "Handler",
    "SubscriptionToken",
    "DEFAULT_REPLAY_BUFFER",
]
