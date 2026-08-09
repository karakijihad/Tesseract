"""Daemon lifecycle — boot/shutdown, heartbeat touch, controller-record
bookkeeping, and the activity-registry seed/forward loops.

Extracted from ``daemon.py`` (module-size cleanup, Task 7.5) as a mixin so
the controller daemon stays a dispatch-table + handlers + lifecycle shell,
matching the existing ``lane_handlers.py`` / ``named_lane_handlers.py``
pattern. ``ControllerDaemon`` inherits this mixin; every method resolves
instance state (``self._server``, ``self._heartbeat_task``, ...) set up in
``daemon.py``'s ``__init__`` via the MRO. The mixin holds no state of its
own beyond what ``__init__`` seeds.

``_now_iso`` moved here too — it was only ever called from ``stop()`` and
``_write_controller_record()``, both below; keeping it here avoids an
import-cycle-prone re-import from ``daemon.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import ActivityEventPush

log = logging.getLogger("tesseract.orchestrator.agent_controller.daemon")


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class _LifecycleMixin:
    """Boot/shutdown, heartbeat, and controller-record helpers. Mixed into
    ``ControllerDaemon``."""

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        # gap-b — seed the registry from disk BEFORE the server accepts any
        # connection, so no client can mutate a live record before the seed
        # runs. This closes the seed-vs-live-event race (a late publish=False
        # seed would otherwise clobber a just-arrived live state back to its
        # disk lifecycle) without needing an existing-id guard. The seed is pure
        # disk I/O and does not depend on the server being up.
        await self._seed_activity_registry()

        self._server = await asyncio.start_server(
            self._handle_client, host=host, port=port
        )
        sockets = self._server.sockets or ()
        if not sockets:
            raise RuntimeError("controller server reported no listening sockets")
        actual_port = sockets[0].getsockname()[1]
        self._address = (host, actual_port)

        self._port_path.parent.mkdir(parents=True, exist_ok=True)
        self._port_path.write_text(str(actual_port), encoding="utf-8")

        self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self._touch_heartbeat()
        self._write_controller_record()

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="controller-heartbeat"
        )
        self._activity_forwarder_task = asyncio.create_task(
            self._activity_forward_loop(), name="controller-activity-forwarder"
        )

    async def _seed_activity_registry(self) -> None:
        """AS-1 gap-b — re-index disk-durable lanes + sessions into the
        controller's OWN Activity registry at boot. After a controller restart
        the in-memory registry is empty, so a live lane's running/idle
        transitions (``update_lane_state``) are silently dropped (no record to
        mutate, see ``registry.update_state``) and a named lane stays under its
        bare ``lane_id`` until the first ``NamedLaneManager.ensure`` re-registers
        it with the human label. Seeding from disk closes that window.

        Reuses the canonical seeder (``publish=False`` — the Mirror seeds its own
        registry independently; the controller only needs the records to EXIST so
        live transitions can mutate + forward them with the correct label).
        Off-loop (disk I/O) and best-effort: a seed failure must never block the
        daemon from accepting clients."""
        try:
            from tesseract.orchestrator.activity.rebuild import rebuild_from_disk

            seeded = await asyncio.to_thread(rebuild_from_disk)
            if seeded:
                log.info(
                    "controller boot: seeded %d activity record(s) from disk", seeded
                )
        except Exception:  # noqa: BLE001 — seeding is best-effort
            log.warning(
                "controller boot: activity registry seed failed", exc_info=True
            )

    async def _activity_forward_loop(self) -> None:
        """AS-1 — subscribe to the controller's `activity` bus channel and
        broadcast each event to every connected client as an
        :class:`ActivityEventPush`. The Mirror's activity subscriber re-applies
        these to the Mirror-side registry. Runs for the daemon's lifetime;
        a forward failure for one event never kills the loop."""
        from tesseract.orchestrator.activity import CHANNEL as ACTIVITY_CHANNEL
        from tesseract.orchestrator.background_event_bus import get_background_bus

        bus = get_background_bus()
        _replay, queue = bus.subscribe()
        try:
            while not self._stop_event.is_set():
                event = await queue.get()
                data = event.data or {}
                if data.get("channel") != ACTIVITY_CHANNEL:
                    continue
                try:
                    await self._broadcast_to_all(
                        ActivityEventPush(envelope=data).model_dump(mode="json")
                    )
                except Exception:  # noqa: BLE001 — one bad forward must not kill the relay
                    log.warning("controller: activity forward failed", exc_info=True)
        except asyncio.CancelledError:
            raise
        finally:
            bus.unsubscribe(queue)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._activity_forwarder_task is not None:
            self._activity_forwarder_task.cancel()
            try:
                await self._activity_forwarder_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._activity_forwarder_task = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._heartbeat_task = None

        if self._server is not None:
            self._server.close()
            # Force-close any client connections still open — `Server.
            # wait_closed()` blocks until every accepted connection detaches,
            # and a client that never sends EOF (crash, stuck test, dropped
            # last message) would otherwise wedge shutdown forever. Closing
            # here makes `_handle_client`'s read raise and its own `finally`
            # do the normal detach/cleanup.
            for conn in list(self._clients.values()):
                if conn.writer is not None:
                    try:
                        conn.writer.close()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

        # Cancel any pending approval futures so brain handlers unblock.
        for fut in list(self._pending_approvals.values()):
            if not fut.done():
                fut.cancel()
        self._pending_approvals.clear()

        # Option B (2026-07-13) — same drain for the parked-asks VIEW. The
        # futures are the same objects already cancelled above (when an ask
        # was currently parked); this clears the now-orphaned view entries
        # too. No settled broadcast here — the server (and its client
        # connections) is already closed by this point.
        for entry in list(self._parked_asks.values()):
            if not entry.future.done():
                entry.future.cancel()
        self._parked_asks.clear()

        # Best-effort: clean up port file. Token + controller.json are left
        # so post-mortem inspection sees the last controller_id.
        try:
            if self._port_path.exists():
                self._port_path.unlink()
        except OSError:
            log.debug("controller: port-file unlink failed", exc_info=True)

        # Mark controller record closed.
        try:
            payload = self._read_controller_record() or {}
            payload["closed_at"] = _now_iso()
            self._atomic_write_json(self._controller_record_path, payload)
        except Exception:  # noqa: BLE001
            log.debug("controller: closed_at stamp failed", exc_info=True)

    # ── heartbeat ──────────────────────────────────────────────────────

    def _touch_heartbeat(self, *, now: float | None = None) -> None:
        path = self._heartbeat_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a"):
            pass
        stamp = now if now is not None else time.time()
        os.utime(path, (stamp, stamp))

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._touch_heartbeat()
            except Exception:  # noqa: BLE001 — never let heartbeat kill the daemon
                log.exception("controller: heartbeat touch failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._heartbeat_interval
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return

    # ── controller record ──────────────────────────────────────────────

    def _write_controller_record(self) -> None:
        payload = {
            "controller_id": self._controller_id,
            "pid": os.getpid(),
            "started_at": _now_iso(),
            "heartbeat_path": str(self._heartbeat_path),
            "ipc_port": int(self._address[1]),
            "token_ref": str(self._port_path.parent / "controller.token"),
            "active_session_ids": [],
        }
        self._atomic_write_json(self._controller_record_path, payload)

    def _read_controller_record(self) -> dict[str, Any] | None:
        path = self._controller_record_path
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _refresh_active_sessions(self) -> None:
        payload = self._read_controller_record() or {}
        payload["active_session_ids"] = sorted(self._sessions_attached.keys())
        payload["ipc_port"] = int(self._address[1])
        self._atomic_write_json(self._controller_record_path, payload)

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, path)
