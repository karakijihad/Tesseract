"""Background event bus — bridges detached schedulers to the websocket layer.

Schedulers (dreaming, drift detection, FAISS rebuilds) run in their own
asyncio tasks with no knowledge of any client connection. They cannot push
events through a websocket directly because they outlive any client session.

This module provides a process-wide pub/sub queue + ring buffer:
  - Publishers call ``publish(event_type, data)`` with no knowledge of subscribers.
  - The orchestrator/server subscribes at startup and forwards events.
  - When no client is connected, events drain into a small ring buffer so a
    fresh subscription can replay recent activity.

Dependency direction is one-way: schedulers → bus ← server. Schedulers
never import websocket code.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RING_SIZE = 200
DEFAULT_QUEUE_SIZE = 100


@dataclass(frozen=True)
class BackgroundEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "data": dict(self.data),
            "timestamp": self.timestamp.isoformat(),
        }


class BackgroundEventBus:
    """Process-wide pub/sub queue with a ring buffer for replay.

    Thread-safety note: this is async-safe (single event loop). It is not
    safe to call ``publish`` from a non-loop thread without wrapping in
    ``loop.call_soon_threadsafe``. Existing schedulers run in the main loop
    or via ``asyncio.to_thread``, then publish from the awaiting coroutine,
    so this is fine for our usage.
    """

    def __init__(
        self,
        ring_size: int = DEFAULT_RING_SIZE,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._ring: deque[BackgroundEvent] = deque(maxlen=ring_size)
        self._subscribers: list[asyncio.Queue[BackgroundEvent]] = []
        self._queue_size = queue_size

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Publish an event. Non-blocking. Safe to call when no subscribers exist."""
        event = BackgroundEvent(type=event_type, data=data or {})
        self._ring.append(event)
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "BackgroundEventBus: subscriber queue full, dropping event %s",
                    event_type,
                )

    def subscribe(self) -> tuple[list[BackgroundEvent], asyncio.Queue[BackgroundEvent]]:
        """Register a new subscriber.

        Returns a snapshot of the ring buffer (for replay) and a fresh queue
        that will receive future events. The caller is responsible for calling
        ``unsubscribe`` when done.
        """
        q: asyncio.Queue[BackgroundEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(q)
        replay = list(self._ring)
        return replay, q

    def unsubscribe(self, queue: asyncio.Queue[BackgroundEvent]) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def snapshot(
        self,
        since: "datetime | None" = None,
        limit: int | None = None,
    ) -> "list[BackgroundEvent]":
        """Return a filtered, ordered slice of the ring buffer.

        Events are returned oldest-first. ``since`` is inclusive.
        """
        events: list[BackgroundEvent] = list(self._ring)
        if since is not None:
            events = [e for e in events if e.timestamp >= since]
        if limit is not None:
            events = events[-limit:]
        return events

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


_bus: BackgroundEventBus | None = None


def get_background_bus() -> BackgroundEventBus:
    """Return the process-wide background event bus singleton."""
    global _bus
    if _bus is None:
        _bus = BackgroundEventBus()
    return _bus


def reset_background_bus() -> None:
    """Reset the singleton. Test-only helper."""
    global _bus
    _bus = None
