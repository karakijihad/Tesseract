"""Phase 2 (deferred) — outbound MCP client reconnect + hot-reload.

Reconnect-on-drop is driven at the supervise-loop level with a stubbed
``_connect_once`` (deterministic, no timing races): a dropped session reconnects
and the circuit breaker trips after ``_MAX_CONSECUTIVE_FAILURES`` failed
attempts. Hot-reload is exercised end-to-end against the real echo subprocess:
enabling a server connects+registers it; disabling it disconnects+unregisters.

All audit writes are pinned under a tmp ``TESSERACT_HOME``.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from tesseract.brain.tools import ToolRegistry
from tesseract.config.mcp_client import (
    MCPClientConfig,
    MCPClientDefaults,
    MCPServerSpec,
)
from tesseract.mcp_client.manager import (
    _MAX_CONSECUTIVE_FAILURES,
    MCPClientManager,
    _ServerHolder,
)
from tesseract.permissions.policy import PermissionPolicy


def _echo_spec() -> MCPServerSpec:
    return MCPServerSpec(
        name="echo_test",
        transport="stdio",
        enabled=True,
        tool_prefix="mcp_echo_",
        command=(sys.executable, "-m", "tesseract.tests.helpers.mcp_echo_server"),
    )


def _config(*servers: MCPServerSpec, connect_timeout_s: int = 30) -> MCPClientConfig:
    return MCPClientConfig(
        defaults=MCPClientDefaults(connect_timeout_s=connect_timeout_s, tool_call_timeout_s=30),
        servers=servers,
    )


def _holder(spec: MCPServerSpec) -> _ServerHolder:
    return _ServerHolder(spec=spec, ready=asyncio.Event(), stop=asyncio.Event())


# ── reconnect (supervise loop, stubbed connect) ─────────────────────────

async def test_breaker_trips_after_consecutive_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    mgr = MCPClientManager(_config(_echo_spec(), connect_timeout_s=1), ToolRegistry(), None)
    holder = _holder(_echo_spec())
    attempts = {"n": 0}

    async def always_fail(h):
        attempts["n"] += 1
        h.ready.set()
        return False  # never establishes

    monkeypatch.setattr(mgr, "_connect_once", always_fail)
    await mgr._serve(holder)  # loops with backoff until the breaker trips
    assert attempts["n"] == _MAX_CONSECUTIVE_FAILURES
    assert holder.session is None


async def test_reconnects_after_a_drop(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    mgr = MCPClientManager(_config(_echo_spec(), connect_timeout_s=1), ToolRegistry(), None)
    holder = _holder(_echo_spec())
    reconnected = asyncio.Event()
    script = ["drop"]  # first attempt establishes then drops; second holds

    async def scripted(h):
        h.ready.set()
        if script:
            script.pop(0)
            return True  # established, then dropped → supervise loop should retry
        reconnected.set()
        await h.stop.wait()
        return True

    monkeypatch.setattr(mgr, "_connect_once", scripted)
    task = asyncio.create_task(mgr._serve(holder))
    try:
        await asyncio.wait_for(reconnected.wait(), timeout=5)  # proves the reconnect happened
    finally:
        holder.stop.set()
        await asyncio.wait_for(task, timeout=5)


# ── hot reload (real echo subprocess) ───────────────────────────────────

async def test_reload_connects_newly_enabled_server(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = ToolRegistry()
    policy = PermissionPolicy({}, {}, {}, "headless")
    # start with nothing enabled
    mgr = MCPClientManager(_config(), registry, policy)
    await mgr.connect_all()
    assert mgr.connected_tool_names() == []
    try:
        result = await mgr.reload(_config(_echo_spec()))
        assert "echo_test" in result["added"]
        assert result["removed"] == []
        assert "mcp_echo_echo" in registry.tools
        assert policy.class_default("mcp_echo_echo") == "ask"
    finally:
        await mgr.shutdown()


async def test_reload_disconnects_removed_server(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = ToolRegistry()
    mgr = MCPClientManager(_config(_echo_spec()), registry, PermissionPolicy({}, {}, {}, "headless"))
    await mgr.connect_all()
    assert "mcp_echo_echo" in registry.tools
    try:
        result = await mgr.reload(_config())  # empty allowlist
        assert "echo_test" in result["removed"]
        assert "mcp_echo_echo" not in registry.tools
        assert mgr.connected_tool_names() == []
    finally:
        await mgr.shutdown()


# ── config-watcher wiring ───────────────────────────────────────────────

def test_config_watcher_wires_mcp_servers_reloader():
    from tesseract.mirror.server.config_watcher import (
        WATCHED_NAMES,
        default_reloaders,
        reload_mcp_servers,
    )

    assert "mcp_servers.yaml" in WATCHED_NAMES
    assert default_reloaders()["mcp_servers.yaml"] is reload_mcp_servers


async def test_reload_mcp_servers_noop_without_manager():
    from aiohttp import web

    from tesseract.mirror.server.config_watcher import reload_mcp_servers

    app = web.Application()
    app["mcp_clients"] = None
    await reload_mcp_servers(app)  # must not raise
