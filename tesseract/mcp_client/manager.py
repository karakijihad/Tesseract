"""``MCPClientManager`` — connect, register, reconnect, and tear down servers.

Lifecycle discipline (the subtle part): the SDK transports and ``ClientSession``
use anyio task groups internally, so each connection MUST be entered and exited
inside a single task. The manager therefore owns one long-lived ``_serve`` task
per server. That task supervises the connection: connect → register → hold
(health-pinging) → on drop, back off and reconnect, all in the same task, until
shutdown unwinds the ``async with`` blocks. Individual ``tools/call`` RPCs run
from other tasks (the chat turn); that is safe because they only push through
anyio memory streams the session's own receive loop drains.

Reconnect (Phase 2 deferred): a dropped session is detected by a periodic
``send_ping`` and re-established with exponential backoff, bounded by a circuit
breaker (the project-wide ``MAX_CONSECUTIVE_FAILURES``). Tools stay registered
across a reconnect — the tool's ``session_provider`` reads the live
``holder.session``, which the supervise loop refreshes.

Hot reload (Phase 2 deferred): ``reload`` diffs the new allowlist against live
holders — tearing down removed/changed servers and connecting added ones —
wired to the Mirror config watcher on ``mcp_servers.yaml`` edits.

Boot wiring (``mirror/server/app.py`` STAGE 2): ``connect_all`` fans out across
enabled servers with per-server isolation — one unreachable server never blocks
boot or the others. Shutdown mirrors the ``mcp_server.stop`` teardown block.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession

from tesseract.config.mcp_client import MCPClientConfig, MCPServerSpec, load_mcp_client_config
from tesseract.mcp_client.remote_tool import MCPRemoteTool
from tesseract.mcp_client.transport import open_transport

log = logging.getLogger(__name__)

_SHUTDOWN_GRACE_S = 5
# Circuit breaker on the reconnect loop — every retry loop trips at 3.
_MAX_CONSECUTIVE_FAILURES = 3
_RECONNECT_BASE_S = 1.0


@dataclass
class _ServerHolder:
    spec: MCPServerSpec
    ready: asyncio.Event
    stop: asyncio.Event
    session: ClientSession | None = None
    remote_tools: list[Any] = field(default_factory=list)
    registered_names: list[str] = field(default_factory=list)
    registered: bool = False
    error: BaseException | None = None
    task: asyncio.Task | None = None


class MCPClientManager:
    def __init__(
        self,
        config: MCPClientConfig,
        registry: Any,
        policy: Any = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._policy = policy
        self._holders: list[_ServerHolder] = []

    @classmethod
    def from_yaml(cls, registry: Any, policy: Any = None) -> "MCPClientManager":
        return cls(load_mcp_client_config(), registry, policy)

    # ── connect ─────────────────────────────────────────────────────────

    async def connect_all(self) -> None:
        """Start a supervise task per ``enabled`` server, in parallel, failure-
        isolated. Returns once every server's first attempt has resolved (bounded
        by ``connect_timeout_s``); reachable servers have registered their tools,
        and a slow/dead server keeps retrying in the background."""
        enabled = self._config.enabled_servers()
        if not enabled:
            log.info("mcp_client: no enabled servers — nothing to connect")
            return
        self._holders = [self._spawn(spec) for spec in enabled]
        await asyncio.gather(
            *(self._await_first(h) for h in self._holders), return_exceptions=True
        )
        for holder in self._holders:
            if not holder.registered:
                log.warning(
                    "mcp_client: server %s not ready at boot (%s) — retrying in background",
                    holder.spec.name,
                    holder.error,
                )

    def _spawn(self, spec: MCPServerSpec) -> _ServerHolder:
        holder = _ServerHolder(spec=spec, ready=asyncio.Event(), stop=asyncio.Event())
        holder.task = asyncio.create_task(self._serve(holder), name=f"mcp-client:{spec.name}")
        return holder

    async def _await_first(self, holder: _ServerHolder) -> None:
        # Wait for the first connect attempt to resolve; never cancel — the
        # supervise loop owns retry/reconnect, boot just stops blocking.
        timeout = self._config.defaults.connect_timeout_s + 1
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(holder.ready.wait(), timeout=timeout)

    async def _serve(self, holder: _ServerHolder) -> None:
        """Supervise one server: (re)connect with backoff + circuit breaker
        until shutdown. Each attempt owns the transport/session lifecycle."""
        failures = 0
        while not holder.stop.is_set():
            established = await self._connect_once(holder)
            holder.ready.set()  # unblock the boot waiter after every attempt
            if holder.stop.is_set():
                break
            if established:
                failures = 0
                log.info("mcp_client: server %s connection dropped — reconnecting", holder.spec.name)
            else:
                failures += 1
                if failures >= _MAX_CONSECUTIVE_FAILURES:
                    log.error(
                        "mcp_client: server %s — %d consecutive connect failures, giving up",
                        holder.spec.name,
                        failures,
                    )
                    break
            delay = min(
                _RECONNECT_BASE_S * (2 ** max(0, failures - 1)),
                float(self._config.defaults.connect_timeout_s),
            )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(holder.stop.wait(), timeout=delay)
        holder.session = None
        holder.ready.set()

    async def _connect_once(self, holder: _ServerHolder) -> bool:
        """One connection attempt. Returns True if a session was established
        (and later dropped or stopped), False if the attempt failed first."""
        spec = holder.spec
        ct = self._config.defaults.connect_timeout_s
        established = False
        try:
            async with open_transport(spec, self._config.defaults) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=ct)
                    listed = await asyncio.wait_for(session.list_tools(), timeout=ct)
                    holder.session = session
                    holder.remote_tools = list(listed.tools)
                    holder.error = None
                    self._register_tools(holder)  # idempotent across reconnects
                    established = True
                    holder.ready.set()
                    await self._health_hold(holder, session)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - isolate this server's failure
            holder.error = exc
            if not established:
                log.warning("mcp_client: server %s connect error: %s", spec.name, exc)
        finally:
            holder.session = None
        return established

    async def _health_hold(self, holder: _ServerHolder, session: ClientSession) -> None:
        """Hold the connection open until shutdown, pinging periodically so a
        silent transport drop surfaces (raising → the supervise loop reconnects)."""
        interval = self._config.defaults.connect_timeout_s
        while not holder.stop.is_set():
            try:
                await asyncio.wait_for(holder.stop.wait(), timeout=interval)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            await asyncio.wait_for(session.send_ping(), timeout=interval)

    # ── register ────────────────────────────────────────────────────────

    def _register_tools(self, holder: _ServerHolder) -> None:
        if holder.registered:
            return
        provider = (lambda h=holder: h.session)
        names: list[str] = []
        for rt in holder.remote_tools:
            tool = MCPRemoteTool(
                server_name=holder.spec.name,
                tool_prefix=holder.spec.tool_prefix,
                remote_name=rt.name,
                description=getattr(rt, "description", "") or "",
                input_schema=getattr(rt, "inputSchema", None),
                session_provider=provider,
                tool_call_timeout_s=self._config.defaults.tool_call_timeout_s,
            )
            # Invariant #8 defense-in-depth: the prefix already namespaces, but
            # never overwrite an existing (core) tool even so.
            if tool.name in self._registry.tools:
                log.error(
                    "mcp_client: server %s tool %r would shadow an existing tool — skipped",
                    holder.spec.name,
                    tool.name,
                )
                continue
            self._registry.register(tool)
            names.append(tool.name)
        holder.registered_names = names
        holder.registered = True
        self._wire_policy_defaults(names)
        log.info(
            "mcp_client: server %s registered %d tool(s): %s",
            holder.spec.name,
            len(names),
            names,
        )

    def _wire_policy_defaults(self, names: list[str]) -> None:
        """Make the ASK floor explicit in the policy so external tools resolve
        without the 'no posture entry' warning and so ``permissions.yaml`` can
        still tighten a specific tool by name. Best-effort — a policy stub
        without ``merge_class_defaults`` (tests) falls back to the ASK default
        in ``resolve_posture``."""
        if self._policy is None or not names:
            return
        merge = getattr(self._policy, "merge_class_defaults", None)
        if merge is None:
            return
        merge({n: MCPRemoteTool.default_posture for n in names})

    # ── hot reload ──────────────────────────────────────────────────────

    async def reload(self, new_config: MCPClientConfig) -> dict[str, list[str]]:
        """Diff the new allowlist against live holders: tear down removed or
        changed servers, connect added ones. Unchanged servers are left alone.
        Returns ``{"added": [...], "removed": [...]}``."""
        self._config = new_config
        new_enabled = {s.name: s for s in new_config.enabled_servers()}

        # Tear down removed/disabled/changed servers concurrently (independent
        # per-server work, so it runs in parallel), then reconcile the
        # holder list after the gather resolves (no await-during-mutation).
        to_remove = [
            h for h in list(self._holders)
            if (new_enabled.get(h.spec.name) is None) or (new_enabled[h.spec.name] != h.spec)
        ]
        await asyncio.gather(
            *(self._shutdown_holder(h) for h in to_remove), return_exceptions=True
        )
        for holder in to_remove:
            self._holders.remove(holder)
        removed = [h.spec.name for h in to_remove]

        live = {h.spec.name for h in self._holders}
        added_holders = [self._spawn(spec) for name, spec in new_enabled.items() if name not in live]
        self._holders.extend(added_holders)
        await asyncio.gather(
            *(self._await_first(h) for h in added_holders), return_exceptions=True
        )
        return {"added": [h.spec.name for h in added_holders], "removed": removed}

    # ── shutdown ────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Close every session (concurrently — independent per-server teardown)
        and unregister its tools."""
        await asyncio.gather(
            *(self._shutdown_holder(h) for h in list(self._holders)),
            return_exceptions=True,
        )
        self._holders = []

    async def _shutdown_holder(self, holder: _ServerHolder) -> None:
        holder.stop.set()
        if holder.task is not None and not holder.task.done():
            try:
                await asyncio.wait_for(holder.task, timeout=_SHUTDOWN_GRACE_S)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                # wait_for cancelled the task on timeout; teardown still ran in-task.
                pass
        for name in holder.registered_names:
            self._registry.tools.pop(name, None)
        holder.registered_names = []
        holder.registered = False

    # ── introspection (tests / status) ──────────────────────────────────

    def connected_tool_names(self) -> list[str]:
        return [n for h in self._holders for n in h.registered_names]


__all__ = ["MCPClientManager"]
