"""Server→client push over the MCP GET SSE stream — the ``activity.watch``
subscription.

``activity.list`` is a snapshot. A client polling it sees the states that
happened to be current when it asked and misses every transition between two
polls — a lane that spawned, ran and closed inside one interval never existed
as far as the poller is concerned. That is the gap this closes; latency is the
lesser half.

The contract, in the order it matters:

* **Scoped.** Every event is filtered through the same ownership rule the
  snapshot uses (``lanes.principals.may_reach``). A stream that pushed
  everything would hand back exactly what D1's filter withholds, on a surface
  with no filter in front of it.
* **Exactly replayable.** Each event carries a monotonic id, prefixed with an
  epoch minted per hub instance. A reconnect with ``Last-Event-ID`` gets every
  event after that cursor, or is told it cannot — never a partial history that
  reads as a complete one. Ids are global, so a caller's own sequence is
  sparse; that is deliberate (an id is an integer, not a record).
* **Bounded.** The shared replay log holds ``replay_buffer`` events; each
  connection holds at most ``client_queue``. A consumer slower than that loses
  its oldest queued events and is told so on its next frame. How many
  connections may exist is bounded too — ``server.max_connections`` gates
  session *creation* only, and a stream is not a session, so without
  ``max_streams_total`` / ``max_streams_per_session`` one authorized client
  could stack queues behind a single session id forever.
* **Revocable.** A stream is bound to an MCP session. Every path that closes a
  session — ``DELETE /mcp``, the idle sweep, ``activity.cancel``, shutdown —
  closes its streams, because the registry calls back into the hub.

A fresh stream replays nothing. ``activity.list`` is the hydration path and
always was; replaying the buffer to a client with no cursor would hand back
records it already holds under ids it cannot reconcile with them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from aiohttp import web

from tesseract.orchestrator.agent_controller.lanes.principals import may_reach

log = logging.getLogger(__name__)

SSE_MIME = "text/event-stream"

# Custom JSON-RPC notifications, vendor-namespaced. An MCP client that does not
# know them ignores them, which is what makes this safe to push unasked.
ACTIVITY_METHOD = "notifications/tesseract/activity"
GAP_METHOD = "notifications/tesseract/activity_gap"

# Advertised on `initialize` under ServerCapabilities.experimental so the
# subscription is discoverable as a protocol rather than as a configured name.
CAPABILITY_KEY = "tesseract/activityStream"
CAPABILITY = {
    "method": ACTIVITY_METHOD,
    "gapMethod": GAP_METHOD,
    "transport": "GET /mcp (text/event-stream)",
    "resume": "Last-Event-ID",
    "hydrate": "activity.list",
}

_CLOSE = object()


@dataclass(frozen=True)
class StreamEvent:
    event_id: int
    owner_principal: str
    shared_with: tuple[str, ...]
    envelope: dict[str, Any]


@dataclass
class StreamSubscriber:
    session_id: str
    caller: str
    queue: asyncio.Queue
    # The last id served from the replay log at open time. `open` attaches the
    # queue and reads the log in one synchronous block, so nothing at or below
    # this can reach the queue and the pump's check never fires today — it is
    # what turns an await slipped into `open` later into a dropped frame rather
    # than a silently doubled one.
    replay_through: int = 0
    # Set the instant the session is revoked. The queue sentinel alone cannot
    # stop a write loop that is not reading the queue — replay is served from a
    # list resolved at `open`, so without this flag a close landing mid-replay
    # keeps writing every remaining frame to a session that no longer exists.
    closed: bool = False
    _gapped: bool = field(default=False, repr=False)

    def mark_gap(self) -> None:
        self._gapped = True

    def take_gap(self) -> bool:
        gapped, self._gapped = self._gapped, False
        return gapped


class ActivityStreamHub:
    """The fan-out behind the SSE streams: one bounded replay log, one bounded
    queue per connection, and the caller-scoping rule applied on both."""

    def __init__(
        self,
        *,
        replay_buffer: int,
        client_queue: int,
        max_streams_total: int,
        max_streams_per_session: int,
    ) -> None:
        self._log: deque[StreamEvent] = deque(maxlen=replay_buffer)
        self._client_queue = client_queue
        self._max_total = max_streams_total
        self._max_per_session = max_streams_per_session
        # Slots are held from BEFORE the response is prepared until after the
        # pump returns, so they bound the whole connection lifetime — not just
        # the window in which a subscriber happens to be attached.
        self._slots: dict[str, int] = {}
        self._slots_total = 0
        self._subs: dict[str, list[StreamSubscriber]] = {}
        self._next_id = 1
        # Ids restart at 1 every boot. Without something naming the process that
        # issued them, a cursor from yesterday silently skips this process's
        # first N events instead of being recognised as unresumable.
        self._epoch = secrets.token_hex(4)

    @property
    def epoch(self) -> str:
        return self._epoch

    @property
    def next_event_id(self) -> int:
        return self._next_id

    @property
    def subscriber_count(self) -> int:
        return sum(len(subs) for subs in self._subs.values())

    @property
    def reserved_count(self) -> int:
        return self._slots_total

    # ── capacity ─────────────────────────────────────────────────────────
    def reserve(self, session_id: str) -> bool:
        """Claim a stream slot for ``session_id``. False when either cap is
        already at its limit — the caller must refuse the connection.

        Synchronous and allocation-free on the reject path, so the check and
        the claim cannot be split by an await and let two concurrent GETs both
        pass a check that only one of them fits through."""
        if self._slots_total >= self._max_total:
            return False
        if self._slots.get(session_id, 0) >= self._max_per_session:
            return False
        self._slots[session_id] = self._slots.get(session_id, 0) + 1
        self._slots_total += 1
        return True

    def release(self, session_id: str) -> None:
        """Return a slot claimed by ``reserve``. Idempotent for an unknown id
        so a double release can never drive the counters negative and wedge the
        cap open."""
        held = self._slots.get(session_id, 0)
        if held <= 0:
            return
        if held == 1:
            self._slots.pop(session_id, None)
        else:
            self._slots[session_id] = held - 1
        self._slots_total -= 1

    # ── publish ──────────────────────────────────────────────────────────
    def publish(self, envelope: dict[str, Any]) -> None:
        """Record one activity envelope and fan it out. Synchronous and
        non-blocking — this is called from ``publish_activity_event``, which
        runs inside a registry mutation."""
        data = envelope.get("data") or {}
        event = StreamEvent(
            event_id=self._next_id,
            owner_principal=str(data.get("owner_principal") or ""),
            shared_with=tuple(data.get("shared_with") or ()),
            envelope=envelope,
        )
        self._next_id += 1
        self._log.append(event)
        for subs in self._subs.values():
            for sub in subs:
                if not _visible(sub.caller, event):
                    continue
                self._offer(sub, event)

    def _offer(self, sub: StreamSubscriber, event: StreamEvent) -> None:
        """Enqueue, dropping this connection's OLDEST event if it is full.

        Dropping the oldest rather than the newest is what makes the gap notice
        land in the right place: the very next frame the pump writes is the
        event that followed the drop, so the notice sits exactly where the
        history breaks."""
        try:
            sub.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        with contextlib.suppress(asyncio.QueueEmpty):
            sub.queue.get_nowait()
        sub.mark_gap()
        with contextlib.suppress(asyncio.QueueFull):
            sub.queue.put_nowait(event)

    # ── subscribe ────────────────────────────────────────────────────────
    def open(
        self, *, session_id: str, caller: str, last_event_id: str | None
    ) -> tuple[StreamSubscriber, list[StreamEvent], str | None]:
        """Attach a subscriber and resolve its replay in one non-async step.

        Doing both here is the point: attaching first means nothing published
        in between is missed, and stamping ``replay_through`` means nothing is
        delivered twice. Returns ``(subscriber, replay, gap_reason)``."""
        sub = StreamSubscriber(
            session_id=session_id,
            caller=caller,
            queue=asyncio.Queue(maxsize=self._client_queue),
            replay_through=self._next_id - 1,
        )
        self._subs.setdefault(session_id, []).append(sub)
        if last_event_id is None:
            return sub, [], None

        epoch, _, raw = last_event_id.rpartition(":")
        if epoch != self._epoch or not raw.isdigit():
            return sub, [], "unknown_epoch"
        cursor = int(raw)
        if cursor > sub.replay_through:
            # A cursor ahead of anything this hub issued — a stale client, or a
            # forged one. Either way there is no history to serve it.
            return sub, [], "unknown_epoch"
        floor = self._log[0].event_id if self._log else self._next_id
        gap = "cursor_expired" if cursor + 1 < floor else None
        replay = [e for e in self._log if e.event_id > cursor and _visible(caller, e)]
        return sub, replay, gap

    def unsubscribe(self, sub: StreamSubscriber) -> None:
        subs = self._subs.get(sub.session_id)
        if subs is None:
            return
        if sub in subs:
            subs.remove(sub)
        if not subs:
            self._subs.pop(sub.session_id, None)

    def close_session(self, session_id: str) -> None:
        """End every stream bound to a session. Called by the session registry,
        so DELETE, the idle sweep and ``activity.cancel`` all reach it."""
        for sub in self._subs.pop(session_id, []):
            self._wake_closed(sub)

    def close_all(self) -> None:
        for session_id in list(self._subs):
            self.close_session(session_id)

    @staticmethod
    def _wake_closed(sub: StreamSubscriber) -> None:
        """Discard whatever is still queued, then deliver the close sentinel.

        Draining first is what makes the close a revocation rather than a
        request to stop soon. The queue is FIFO and the pump only checks for
        the sentinel by dequeuing it, so a sentinel appended behind N pending
        events means those N are still written to a client whose session has
        already been closed by DELETE, the idle sweep, ``activity.cancel`` or
        shutdown. They were authorized when they were queued; they are not
        authorized now, and the caller cannot tell the difference.

        ``closed`` is set for the same reason and covers what the queue cannot:
        the replay loop writes from a list resolved at ``open`` and never
        touches the queue, so a sentinel is invisible to it. Without the flag a
        close landing mid-replay keeps writing up to ``replay_buffer`` more
        frames.

        What this cannot undo is a frame already handed to ``resp.write`` —
        the pump may be suspended inside one. That is one event wide and
        bounded by the write, where the queue and the replay list were each
        bounded only by their configured size.

        A ``consumer_lag`` notice the subscriber had earned but not yet
        rendered dies with the drain. Deliberate: the notice exists to send a
        client back to ``activity.list``, and a revoked stream sends it there
        regardless — there is no history left for it to be a gap in.
        """
        sub.closed = True
        while True:
            try:
                sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        with contextlib.suppress(asyncio.QueueFull):
            sub.queue.put_nowait(_CLOSE)


def _visible(caller: str, event: StreamEvent) -> bool:
    return may_reach(
        caller=caller, owner=event.owner_principal, shared_with=event.shared_with
    )


# ── the SSE pump ─────────────────────────────────────────────────────────


def _frame(payload: dict[str, Any], event_id: str | None = None) -> bytes:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    # `json.dumps` escapes newlines, so the payload can never break out of its
    # own `data:` line and forge a second SSE field.
    lines.append(f"data: {json.dumps(payload, separators=(',', ':'))}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def _gap_frame(reason: str) -> bytes:
    """A gap notice carries no id. It is not a logged event, so advancing the
    client's cursor past it would skip the first real event after the break."""
    return _frame(
        _notification(
            GAP_METHOD,
            {
                "reason": reason,
                "resync": "activity.list",
                "detail": (
                    "events were dropped for this connection; the snapshot is "
                    "the only exact recovery"
                ),
            },
        )
    )


async def serve_activity_stream(
    request: web.Request,
    *,
    hub: ActivityStreamHub,
    session_id: str,
    caller: str,
    heartbeat_s: float,
    last_event_id: str | None,
    session_is_live: Callable[[], bool],
    on_activity: Callable[[], None] | None = None,
) -> web.StreamResponse:
    """Run one SSE connection to completion. Returns when the session closes,
    the client disconnects, or the server shuts down.

    ``session_is_live`` is re-checked immediately before subscribing, with no
    await in between. The handler validated the session several awaits ago —
    approval, audit, ``prepare`` — and a close landing in that window finds no
    subscriber to revoke, so without this the subscription outlives the session
    that authorized it and nothing can ever take it down."""
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": f"{SSE_MIME}; charset=utf-8",
            "Cache-Control": "no-cache, no-store",
            # Proxies that buffer would defeat the whole point; the endpoint is
            # loopback today, but the header costs nothing and travels with it.
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)
    if not session_is_live():
        with contextlib.suppress(Exception):
            await resp.write_eof()
        return resp
    sub, replay, gap = hub.open(
        session_id=session_id, caller=caller, last_event_id=last_event_id
    )
    async def send(payload: bytes) -> bool:
        """Write one frame unless the session has been revoked. False once it
        has, which every caller treats as "stop".

        The check belongs to the write rather than sitting beside it. The
        critical this shape replaced was one missed re-read — the replay loop
        served a list resolved at ``open`` and never consulted the queue, so
        the close sentinel was invisible to it and every remaining frame went
        out. Enforcing the invariant per-call means the next await added to
        this pump inherits it instead of having to remember it.
        """
        if sub.closed:
            return False
        await resp.write(payload)
        return True

    try:
        if gap is not None:
            await send(_gap_frame(gap))
        for event in replay:
            if not await send(
                _frame(
                    _notification(ACTIVITY_METHOD, event.envelope),
                    f"{hub.epoch}:{event.event_id}",
                )
            ):
                break
        while True:
            try:
                item = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat_s)
            except asyncio.TimeoutError:
                # An SSE comment: keeps the connection warm and, via
                # `on_activity`, keeps a watch-only session off the idle sweep.
                if not await send(b": ping\n\n"):
                    break
                if on_activity is not None:
                    on_activity()
                continue
            if item is _CLOSE:
                break
            if item.event_id <= sub.replay_through:
                continue  # already served from the replay log
            if sub.take_gap() and not await send(_gap_frame("consumer_lag")):
                break
            if not await send(
                _frame(
                    _notification(ACTIVITY_METHOD, item.envelope),
                    f"{hub.epoch}:{item.event_id}",
                )
            ):
                break
            if on_activity is not None:
                on_activity()
    except (ConnectionResetError, ConnectionAbortedError):
        log.debug("mcp activity stream: client vanished (%s)", session_id)
    finally:
        hub.unsubscribe(sub)
        with contextlib.suppress(Exception):
            await resp.write_eof()
    return resp


__all__ = [
    "ACTIVITY_METHOD",
    "CAPABILITY",
    "CAPABILITY_KEY",
    "GAP_METHOD",
    "SSE_MIME",
    "ActivityStreamHub",
    "StreamEvent",
    "StreamSubscriber",
    "serve_activity_stream",
]
