"""Bind a principal to an in-process `LaneManager`, the way the IPC proxy does.

`IpcLaneManager` carries `caller_principal` on the instance, so a kernel tool
reaching the lane surface over IPC has no argument with which to name a
principal and cannot pick one. Sessions hosted *inside* the controller daemon
share a process with the real `LaneManager` and were handed it raw — and every
kernel `lane_*` tool calls the manager positionally, without `caller=`, which
`LaneManager._authorize` reads as "no principal boundary" and allows.

That is correct for the daemon's own recovery and cleanup paths, which act on
every lane by definition. It is wrong for a session created on behalf of an
MCP client through `agent.assign`: that session's tools would hold cross-lane
administration no `lane.*` verb would ever have granted them.

This facade closes the gap without touching a single tool. Same shape as the
IPC proxy, same invariant: the identity lives on the manager instance, and the
caller cannot choose it.
"""

from __future__ import annotations

from typing import Any

from .models import LaneKind, LaneMode


class ScopedLaneManager:
    """A `LaneManager` that speaks for exactly one principal."""

    def __init__(self, inner: Any, caller_principal: str) -> None:
        self._inner = inner
        self._caller = caller_principal

    @property
    def _runtimes(self) -> Any:
        """`NamedLaneManager.ensure` probes this to decide whether a reused
        binding needs re-attaching after a daemon restart. Forwarded so the
        facade cannot silently disable that self-heal if someone ever wraps
        the managers the other way round — the failure would be a lane that
        reports "not attached" again, with nothing raised to explain it."""
        return getattr(self._inner, "_runtimes", None)

    async def open(
        self,
        *,
        kind: LaneKind,
        mode: LaneMode = "headless",
        model: str,
        working_dir: str,
        env: dict[str, str] | None = None,
        read_only: bool = False,
        shared_with: tuple[str, ...] | list[str] = (),
    ) -> str:
        return await self._inner.open(
            kind=kind,
            mode=mode,
            model=model,
            working_dir=working_dir,
            env=env,
            read_only=read_only,
            owner_principal=self._caller,
            shared_with=shared_with,
        )

    async def send(self, lane_id: str, message: str) -> Any:
        return await self._inner.send(lane_id, message, caller=self._caller)

    def read(self, lane_id: str, since_cursor: str | None = None) -> Any:
        return self._inner.read(lane_id, since_cursor, caller=self._caller)

    def status(self, lane_id: str) -> Any:
        return self._inner.status(lane_id, caller=self._caller)

    async def attach(self, lane_id: str) -> Any:
        return await self._inner.attach(lane_id, caller=self._caller)

    async def close(self, lane_id: str, reason: str) -> Any:
        return await self._inner.close(lane_id, reason, caller=self._caller)

    async def interrupt(self, lane_id: str, turn_id: str | None = None) -> bool:
        return await self._inner.interrupt(lane_id, turn_id, caller=self._caller)

    def list_ids(self) -> list[str]:
        return self._inner.list_ids(caller=self._caller)

    def poll_turn(
        self, lane_id: str, turn_id: str, since_cursor: str | None = None
    ) -> Any:
        return self._inner.poll_turn(
            lane_id, turn_id, since_cursor, caller=self._caller
        )

    async def await_turn(self, lane_id: str, turn_id: str, **kw: Any) -> Any:
        return await self._inner.await_turn(
            lane_id, turn_id, caller=self._caller, **kw
        )

    async def send_and_await(self, lane_id: str, message: str, **kw: Any) -> Any:
        return await self._inner.send_and_await(
            lane_id, message, caller=self._caller, **kw
        )

    async def drain(self, lane_id: str) -> None:
        await self._inner.drain(lane_id)


class ScopedNamedLaneManager:
    """`NamedLaneManager.ensure` on behalf of one principal.

    `get` and `list` pass straight through: a named binding is just a shared
    label (`coder/claude`, `auditor/codex`), and the map it returns is not a
    way around the owner check — every operation on an id it hands back still
    authorizes against the lane's persisted owner.
    """

    def __init__(self, inner: Any, caller_principal: str) -> None:
        self._inner = inner
        self._caller = caller_principal

    async def ensure(self, name: str, **kw: Any) -> Any:
        return await self._inner.ensure(
            name, owner_principal=self._caller, **kw
        )

    def get(self, name: str) -> Any:
        return self._inner.get(name)

    def list(self) -> Any:
        return self._inner.list()


__all__ = ["ScopedLaneManager", "ScopedNamedLaneManager"]
