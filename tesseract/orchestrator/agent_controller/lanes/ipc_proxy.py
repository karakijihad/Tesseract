"""IPC-backed LaneManager proxy for the Mirror chat brain (conductor bridge).

The controller daemon owns the real LaneManager. Mirror runs in a sibling
process, so its lane_* tools resolve THIS proxy: each call opens a fresh
ControllerClient (routes/lanes.py pattern), forwards over IPC, reconstructs
the typed LaneManager return shape, and closes the connection. Controller
offline -> LaneManagerError so the tool degrades cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
from inspect import isawaitable
from typing import Any, AsyncIterator, Awaitable, Callable  # noqa: F401

from .manager import LaneManagerError, LaneTurnNotFoundError
from .models import (
    LaneEvent,
    LaneKind,
    LaneMode,
    LaneSendResult,
    LaneSnapshot,
    LaneStatus,
    TurnOutcome,
)
from .named import NamedLaneRecord
from .turn_wait import TurnAccumulator, TurnPoll

ConnectFactory = Callable[[], Awaitable[Any]]


class _IpcBase:
    """`caller_principal` is bound to the proxy, not passed per call.

    The Mirror resolves the MCP bearer token to a client identity once, at the
    gateway, and builds the proxy with it — so a kernel tool has no argument
    with which to name a principal, and cannot pick one. `None` is the
    in-process default and the daemon refuses it; every real caller is
    constructed with an identity."""

    def __init__(
        self,
        *,
        connect_factory: ConnectFactory | None = None,
        caller_principal: str | None = None,
    ) -> None:
        self._connect_factory = connect_factory
        self._caller_principal = caller_principal

    async def _connect(self) -> Any:
        if self._connect_factory is not None:
            return await self._connect_factory()
        from ..ipc_client import ControllerClient
        return await ControllerClient.connect()

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        """One connection, held for the duration of the block.

        Every call in this module used to open and close its own — fine for
        a single request, ruinous for a poll loop: at `poll_s: 0.5` one
        active lane is ~2 handshakes/sec, a relay ~4, before lenses. The
        `finally` is what makes a cancelled waiter give its socket back
        (`asyncio.CancelledError` is a BaseException in 3.12, so a bare
        `except Exception` would leak it)."""
        try:
            client = await self._connect()
        except Exception as exc:  # noqa: BLE001 — offline / handshake failure
            raise LaneManagerError(f"controller_offline: {exc}") from exc
        try:
            yield client
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _invoke(client: Any, fn_name: str, *args: Any, **kwargs: Any) -> Any:
        from ..ipc_client import ControllerClientError

        try:
            result = getattr(client, fn_name)(*args, **kwargs)
            return await result if isawaitable(result) else result
        except ControllerClientError as exc:
            raise LaneManagerError(str(exc)) from exc

    async def _call(self, fn_name: str, *args: Any, **kwargs: Any) -> Any:
        async with self._session() as client:
            return await self._invoke(client, fn_name, *args, **kwargs)


class IpcLaneManager(_IpcBase):
    async def open(self, *, kind: LaneKind, mode: LaneMode = "headless", model: str,
                   working_dir: str, env: dict[str, str] | None = None,
                   read_only: bool = False,
                   shared_with: list[str] | tuple[str, ...] = ()) -> str:
        return await self._call("lane_open", kind=kind, mode=mode, model=model,
                                working_dir=working_dir, env=env,
                                read_only=read_only,
                                shared_with=list(shared_with),
                                caller_principal=self._caller_principal)

    async def send(self, lane_id: str, message: str) -> LaneSendResult:
        d = await self._call("lane_send", lane_id, message,
                             caller_principal=self._caller_principal)
        return LaneSendResult.model_validate(d)

    async def read(self, lane_id: str,
                   since_cursor: str | None = None) -> tuple[list[LaneEvent], str]:
        d = await self._call("lane_read", lane_id, since_cursor,
                             caller_principal=self._caller_principal)
        events = [LaneEvent.model_validate(e) for e in d.get("events", [])]
        return events, str(d.get("next_cursor") or "")

    async def status(self, lane_id: str) -> LaneStatus:
        d = await self._call("lane_status", lane_id,
                             caller_principal=self._caller_principal)
        return LaneStatus.model_validate(d)

    async def attach(self, lane_id: str) -> LaneSnapshot:
        d = await self._call("lane_attach", lane_id,
                             caller_principal=self._caller_principal)
        return LaneSnapshot.model_validate(d)

    async def close(self, lane_id: str, reason: str) -> dict[str, Any]:
        return await self._call("lane_close", lane_id, reason,
                                caller_principal=self._caller_principal)

    async def interrupt(self, lane_id: str, turn_id: str | None = None) -> bool:
        # Cancel a turn (steer) via the daemon. `turn_id` scopes it to one
        # turn so a handle-scoped cancel cannot reach a sibling.
        d = await self._call("lane_interrupt", lane_id, turn_id,
                             caller_principal=self._caller_principal)
        return bool(d.get("interrupted")) if isinstance(d, dict) else False

    async def list_ids(self) -> list[str]:
        return await self._call(
            "lane_list", caller_principal=self._caller_principal
        )

    async def await_turn(self, lane_id: str, turn_id: str, *, timeout: float,
                         poll_s: float = 0.5,
                         since_cursor: str | None = None,
                         on_events: Callable[[list[LaneEvent]],
                                             Awaitable[None]] | None = None
                         ) -> TurnOutcome:
        """Wait for ONE named turn over IPC. Mirrors
        `LaneManager.await_turn`, including failing closed on an unknown id.

        One connection for the whole wait, not one per poll. The daemon does
        the correlation (`lane_turn_read`); this loop only accumulates and
        decides when the lane has gone quiet for too long."""
        acc = TurnAccumulator(
            lane_id=lane_id, turn_id=turn_id, cursor=since_cursor or ""
        )
        # None on the first poll means "start from the turn's submission
        # cursor", which only the daemon knows. After that the daemon's own
        # next_cursor drives the read.
        cursor: str | None = since_cursor
        async with self._session() as client:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                payload = await self._invoke(
                    client, "lane_turn_read", lane_id, turn_id, cursor,
                    caller_principal=self._caller_principal,
                )
                poll = TurnPoll.model_validate(payload or {})
                if not poll.known:
                    raise LaneTurnNotFoundError(
                        f"lane {lane_id} never issued turn {turn_id!r}; "
                        f"a waiter must name the turn its own send returned"
                    )
                cursor = poll.cursor
                if acc.absorb(poll):
                    deadline = loop.time() + timeout
                if poll.events and on_events is not None:
                    await on_events(poll.events)
                if acc.completed:
                    break
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(poll_s)
        return acc.outcome()

    async def send_and_await(self, lane_id: str, message: str, *,
                             timeout: float, poll_s: float = 0.5) -> LaneSendResult:
        """Send, then wait for THAT turn. `timeout` bounds SILENCE (stall),
        not total turn duration — a wall-clock cap abandons healthy long
        turns (2026-07-13 incident).

        The wait's verdict rides back on the result, so `lane_send(wait=True)`
        can tell a completion from a stall instead of reporting an
        acceptance either way."""
        result = await self.send(lane_id, message)
        if not result.accepted or not result.turn_id:
            return result
        outcome = await self.await_turn(
            lane_id, result.turn_id, timeout=timeout, poll_s=poll_s
        )
        return result.model_copy(update={"outcome": outcome})


class IpcNamedLaneManager(_IpcBase):
    async def ensure(
        self,
        name: str,
        *,
        kind: LaneKind,
        model: str,
        working_dir: str | None = None,
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
            caller_principal=self._caller_principal,
        )
        return NamedLaneRecord.model_validate(d)

    async def get(self, name: str) -> NamedLaneRecord | None:
        d = await self._call("lane_named_get", name,
                             caller_principal=self._caller_principal)
        return NamedLaneRecord.model_validate(d) if d else None

    async def list(self) -> list[NamedLaneRecord]:
        rows = await self._call("lane_named_list",
                                caller_principal=self._caller_principal)
        return [NamedLaneRecord.model_validate(r) for r in rows]
