"""AS-1 — controller→Mirror activity push subscriber.

The Mirror reflects everything running, but lanes + controller sessions live
in the controller daemon (a separate OS process with its own event bus). This
holds ONE always-on :class:`ControllerClient` connection that receives the
daemon's broadcast ``activity_event`` pushes and re-applies each to the
Mirror-side activity registry — whose own publish then drives the per-WS
``activity`` pump out to the operator.

No session attach: activity is broadcast to every authenticated client, so the
subscriber starts receiving immediately after auth. Resilient to the
controller being down at boot or restarting mid-session — it reconnects with
capped backoff. Delegates (Mirror-process) and the boot disk-rebuild populate
the registry independently; this subscriber only carries the cross-process
half.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from tesseract.orchestrator.activity import (
    ActivityRecord,
    get_activity_registry,
)
from tesseract.orchestrator.activity.registry import ActivityRegistry
from tesseract.orchestrator.tars_controller.ipc_client import (
    ControllerClient,
    ControllerClientError,
)

log = logging.getLogger(__name__)

_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 10.0


class ActivitySubscriber:
    """Owns the long-lived controller connection + reconnect loop."""

    def __init__(
        self,
        *,
        registry: ActivityRegistry | None = None,
        connect: Callable[[], Awaitable[Any]] | None = None,
        backoff_initial: float = _BACKOFF_INITIAL_S,
        backoff_max: float = _BACKOFF_MAX_S,
        parked_store: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._registry = registry or get_activity_registry()
        self._connect = connect or ControllerClient.connect
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Option B (2026-07-13) — Mirror-side VIEW of the controller's
        # `_parked_asks`. Mutated in place so `app["controller_parked_asks"]`
        # (the caller's dict) stays the single reference `routes/asks_parked.
        # py` reads from; never authoritative — the daemon's future is.
        self._parked_store = parked_store
        # The live `ControllerClient` while connected, else None. Exposed via
        # the `client` property so `routes/asks_parked.py` can relay a
        # `decide_parked_ask` over the SAME subscriber connection instead of
        # opening a second, competing one.
        self._client: Any | None = None

    @property
    def client(self) -> Any | None:
        """The live controller connection, or None while disconnected."""
        return self._client

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="activity-subscriber")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        backoff = self._backoff_initial
        while not self._stop.is_set():
            try:
                client = await self._connect()
            except ControllerClientError:
                # Controller down — back off and retry (quiet; this is the
                # expected state when the daemon hasn't booted yet).
                if await self._sleep_or_stop(backoff):
                    return
                backoff = min(backoff * 2, self._backoff_max)
                continue
            except Exception:  # noqa: BLE001 — unexpected connect failure
                log.warning("activity subscriber: connect raised", exc_info=True)
                if await self._sleep_or_stop(backoff):
                    return
                backoff = min(backoff * 2, self._backoff_max)
                continue

            backoff = self._backoff_initial  # reset on a good connection
            self._client = client
            try:
                # gap-a — reconcile the full controller set BEFORE streaming
                # deltas, so a lane/session mid-flight at connect time shows its
                # real state immediately instead of stale disk-seeded state.
                await client.request_snapshot()
                try:
                    # Best-effort: an older/stub client without this method
                    # (or a transient send failure) must not abort activity
                    # sync, which is the primary job of this connection.
                    await client.request_parked_asks_snapshot()
                except AttributeError:
                    pass
                async for push in client.pushes():
                    if self._stop.is_set():
                        break
                    event = push.get("event")
                    if event == "_disconnected":
                        break
                    if event == "activity_snapshot":
                        self._apply_snapshot(push.get("records") or [])
                        continue
                    if event == "parked_asks_snapshot":
                        self._apply_parked_snapshot(push.get("items") or [])
                        continue
                    if event == "controller_ask_parked":
                        self._upsert_parked(push)
                        continue
                    if event == "controller_ask_settled":
                        self._remove_parked(push.get("approval_id"))
                        continue
                    if event != "activity_event":
                        continue
                    self._apply(push.get("envelope") or {})
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — push-loop error → reconnect
                log.warning("activity subscriber: push loop error", exc_info=True)
            finally:
                self._client = None
                # Controller unreachable — an un-actionable stale parked-ask
                # card (decide_parked_ask would just 503) is worse than a
                # momentary gap; the next reconnect's snapshot repopulates it.
                if self._parked_store is not None:
                    self._parked_store.clear()
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
            if await self._sleep_or_stop(backoff):
                return

    async def _sleep_or_stop(self, delay: float) -> bool:
        """Sleep up to ``delay`` seconds; return True if stop was requested."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    @staticmethod
    def _record_from_data(data: dict[str, Any]) -> ActivityRecord:
        """Build an ``ActivityRecord`` from a flat wire-data dict (the `data`
        of an activity envelope, or one entry of a snapshot's `records`)."""
        return ActivityRecord(
            activity_id=data["activity_id"],
            kind=data["kind"],
            label=data.get("label", ""),
            state=data["state"],
            durability=data["durability"],
            provider=data.get("provider"),
            parent_turn_id=data.get("parent_turn_id"),
            parent_session_id=data.get("parent_session_id"),
            transcript_ref=data.get("transcript_ref"),
            goal=data.get("goal"),
            result=data.get("result"),
            started_at=data.get("started_at", ""),
            updated_at=data.get("updated_at", ""),
        )

    def _apply(self, envelope: dict[str, Any]) -> None:
        """Translate one controller activity envelope into a Mirror-registry
        mutation. The registry's own publish drives the WS pump."""
        try:
            kind = envelope.get("kind")
            record = self._record_from_data(envelope.get("data") or {})
        except Exception:  # noqa: BLE001 — malformed push, drop it
            log.warning(
                "activity subscriber: bad envelope %r", envelope, exc_info=True
            )
            return
        if kind == "activity_removed":
            self._registry.remove(record.activity_id)
        else:  # activity_registered / activity_updated → upsert
            self._registry.register(record)

    def _apply_snapshot(self, records: list[dict[str, Any]]) -> None:
        """gap-a — reconcile the Mirror registry to the controller's full set on
        (re)connect. Upsert-ONLY: the controller snapshot holds only
        controller-side records (lanes/sessions); Mirror-process delegates live
        in the same registry but are absent here, so a remove-reconcile would
        wrongly evict them. A malformed entry is dropped individually so one bad
        record can't sink the whole snapshot."""
        for data in records:
            try:
                record = self._record_from_data(data)
            except Exception:  # noqa: BLE001 — drop the bad entry, keep the rest
                log.warning(
                    "activity subscriber: bad snapshot record %r", data, exc_info=True
                )
                continue
            self._registry.register(record)

    # ── controller-side ASK parking (Option B, 2026-07-13) ──────────────

    def _upsert_parked(self, data: dict[str, Any]) -> None:
        """Normalize a `ControllerAskParkedPush` payload (or one entry of a
        `parked_asks_snapshot`'s `items`, same field names) into the merged
        wire shape `routes/asks_parked.py` returns alongside chat-origin
        `ParkedAsk.to_wire()` entries."""
        if self._parked_store is None:
            return
        approval_id = data.get("approval_id")
        if not isinstance(approval_id, str):
            log.warning("activity subscriber: bad parked-ask payload %r", data)
            return
        self._parked_store[approval_id] = {
            "approval_id": approval_id,
            "call_id": data.get("tool_use_id", ""),
            "session_id": data.get("session_id", ""),
            "tool_name": data.get("tool", ""),
            "input_summary": data.get("summary", ""),
            "spawn_handle_id": None,
            "parked_at": data.get("parked_at", ""),
            "origin": "controller",
        }

    def _apply_parked_snapshot(self, items: list[dict[str, Any]]) -> None:
        """Full-replace reconcile on (re)connect — unlike `_apply_snapshot`
        (upsert-only), the controller is the SOLE source for this store, so
        a stale entry left over from before a reconnect is safe to drop."""
        if self._parked_store is None:
            return
        self._parked_store.clear()
        for item in items:
            self._upsert_parked(item)

    def _remove_parked(self, approval_id: Any) -> None:
        if self._parked_store is None or not isinstance(approval_id, str):
            return
        self._parked_store.pop(approval_id, None)


__all__ = ["ActivitySubscriber"]
