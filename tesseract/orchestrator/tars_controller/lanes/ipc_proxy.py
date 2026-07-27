"""IPC-backed LaneManager proxy for the Mirror chat brain (conductor bridge).

The controller daemon owns the real LaneManager. Mirror runs in a sibling
process, so its lane_* tools resolve THIS proxy: each call opens a fresh
ControllerClient (routes/lanes.py pattern), forwards over IPC, reconstructs
the typed LaneManager return shape, and closes the connection. Controller
offline -> LaneManagerError so the tool degrades cleanly.
"""

from __future__ import annotations

import asyncio
from inspect import isawaitable
from typing import Any, Awaitable, Callable

from .manager import LaneManagerError
from .models import LaneEvent, LaneKind, LaneMode, LaneSendResult, LaneSnapshot, LaneStatus
from .named import NamedLaneRecord

ConnectFactory = Callable[[], Awaitable[Any]]


class _IpcBase:
    def __init__(self, *, connect_factory: ConnectFactory | None = None) -> None:
        self._connect_factory = connect_factory

    async def _connect(self) -> Any:
        if self._connect_factory is not None:
            return await self._connect_factory()
        from ..ipc_client import ControllerClient
        return await ControllerClient.connect()

    async def _call(self, fn_name: str, *args: Any, **kwargs: Any) -> Any:
        from ..ipc_client import ControllerClientError
        try:
            client = await self._connect()
        except Exception as exc:  # noqa: BLE001 — offline / handshake failure
            raise LaneManagerError(f"controller_offline: {exc}") from exc
        try:
            result = getattr(client, fn_name)(*args, **kwargs)
            return await result if isawaitable(result) else result
        except ControllerClientError as exc:
            raise LaneManagerError(str(exc)) from exc
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass


class IpcLaneManager(_IpcBase):
    async def open(self, *, kind: LaneKind, mode: LaneMode = "headless", model: str,
                   working_dir: str, env: dict[str, str] | None = None) -> str:
        return await self._call("lane_open", kind=kind, mode=mode, model=model,
                                working_dir=working_dir, env=env)

    async def send(self, lane_id: str, message: str) -> LaneSendResult:
        d = await self._call("lane_send", lane_id, message)
        return LaneSendResult.model_validate(d)

    async def read(self, lane_id: str,
                   since_cursor: str | None = None) -> tuple[list[LaneEvent], str]:
        d = await self._call("lane_read", lane_id, since_cursor)
        events = [LaneEvent.model_validate(e) for e in d.get("events", [])]
        return events, str(d.get("next_cursor") or "")

    async def status(self, lane_id: str) -> LaneStatus:
        d = await self._call("lane_status", lane_id)
        return LaneStatus.model_validate(d)

    async def attach(self, lane_id: str) -> LaneSnapshot:
        d = await self._call("lane_attach", lane_id)
        return LaneSnapshot.model_validate(d)

    async def close(self, lane_id: str, reason: str) -> dict[str, Any]:
        return await self._call("lane_close", lane_id, reason)

    async def interrupt(self, lane_id: str) -> bool:
        # M2 — cancel the lane's in-flight turn (steer) via the daemon.
        d = await self._call("lane_interrupt", lane_id)
        return bool(d.get("interrupted")) if isinstance(d, dict) else False

    async def list_ids(self) -> list[str]:
        return await self._call("lane_list")

    async def send_and_await(self, lane_id: str, message: str, *,
                             timeout: float, poll_s: float = 0.5) -> LaneSendResult:
        """Send then poll read() until a turn_ended event arrives, yielding the
        event loop between polls. `timeout` bounds SILENCE (stall), not total
        turn duration — lane activity extends the wait (2026-07-13 incident:
        wall-clock caps abandoned healthy long turns). On stall, returns the
        send result — caller can read remaining events via lane_read.

        NOTE: each poll opens a fresh ControllerClient (per-call connect
        pattern of _IpcBase). Acceptable for MVP; a persistent connection would
        reduce overhead at high poll rates."""
        _, cursor = await self.read(lane_id, None)   # capture current tail
        result = await self.send(lane_id, message)
        if not result.accepted:
            return result
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            events, cursor = await self.read(lane_id, cursor)
            if events:
                deadline = loop.time() + timeout
            if any(e.kind == "turn_ended" for e in events):
                return result
            await asyncio.sleep(poll_s)
        return result


class IpcNamedLaneManager(_IpcBase):
    async def ensure(
        self,
        name: str,
        *,
        kind: LaneKind,
        model: str,
        working_dir: str,
        mode: LaneMode = "headless",
        env: dict[str, str] | None = None,  # noqa: ARG002
    ) -> NamedLaneRecord:
        # env is not forwarded: ControllerClient.lane_named_ensure / the IPC wire do not
        # carry it (named lanes get controller-side env handling). Accepted for signature
        # parity only.
        d = await self._call(
            "lane_named_ensure",
            name=name,
            kind=kind,
            model=model,
            working_dir=working_dir,
            mode=mode,
        )
        return NamedLaneRecord.model_validate(d)

    async def get(self, name: str) -> NamedLaneRecord | None:
        d = await self._call("lane_named_get", name)
        return NamedLaneRecord.model_validate(d) if d else None

    async def list(self) -> list[NamedLaneRecord]:
        rows = await self._call("lane_named_list")
        return [NamedLaneRecord.model_validate(r) for r in rows]
