"""Capability-growth Phase 3 — capability routing for external MCP tools.

Phase 3 is a *verification* pass: Phase 2 already registers every external MCP
tool as `tier="extended"` (`mcp_client/remote_tool.py::MCPRemoteTool`), and the
existing tiering + `tool_search` funnel (`brain/tools.py::schemas_for_adapter`,
`kernel/tools/tool_search.py`) is tier-blind on execution. These regression
tests lock the *selection funnel* for MCP tools against the live boot registry
and the real disposable echo server, so a later refactor can't silently break:

  1. An MCP tool is discoverable via `tool_search` and callable the next turn
     (search -> enable -> visible in `schemas_for_adapter` -> `execute_tool`).
  2. Session-start schemas stay CORE-ONLY with MCP tools registered — no MCP
     tool leaks into the always-visible core surface (progressive disclosure /
     schema budget).
  3. Representative capability queries select the correct tool, including
     description-only recall and MCP-vs-core disambiguation (`tool_search`
     returns only extended tools; core stays visible independently).

All audit/log writes are pinned under a tmp `TESSERACT_HOME`; the echo
subprocess is always torn down via `manager.shutdown()`.
"""

from __future__ import annotations

import sys

import pytest

from tesseract.brain.tools import ToolRegistry, execute_tool
from tesseract.config.mcp_client import (
    MCPClientConfig,
    MCPClientDefaults,
    MCPServerSpec,
)
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.tool_search import ToolSearchTool
from tesseract.mcp_client.manager import MCPClientManager
from tesseract.permissions.policy import PermissionPolicy


def _echo_spec() -> MCPServerSpec:
    return MCPServerSpec(
        name="echo_test",
        transport="stdio",
        enabled=True,
        tool_prefix="mcp_echo_",
        command=(sys.executable, "-m", "tesseract.tests.helpers.mcp_echo_server"),
    )


def _config() -> MCPClientConfig:
    return MCPClientConfig(
        defaults=MCPClientDefaults(connect_timeout_s=30, tool_call_timeout_s=30),
        servers=(_echo_spec(),),
    )


def _context(registry: ToolRegistry, root: str, enabled: set[str]) -> ToolContext:
    return ToolContext(
        workspace_root=root,
        session_id="capgrowth-p3",
        current_call_id="call-1",
        tool_registry_provider=lambda: registry,
        enabled_extended_tools=enabled,
    )


async def _connected_manager(
    registry: ToolRegistry, tmp_path
) -> MCPClientManager:
    # tool_search is a CORE tool in the live boot registry; a bare test
    # registry must register it explicitly to exercise the funnel.
    if "tool_search" not in registry.tools:
        registry.register(ToolSearchTool())
    policy = PermissionPolicy({}, {}, {}, "headless")
    mgr = MCPClientManager(_config(), registry, policy)
    await mgr.connect_all()
    assert "mcp_echo_echo" in registry.tools, "echo server did not register its tools"
    return mgr


# ── 1. discoverable via tool_search + callable next turn ────────────────────


async def test_mcp_tool_discoverable_via_search_and_callable_next_turn(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = ToolRegistry()
    mgr = await _connected_manager(registry, tmp_path)
    try:
        enabled: set[str] = set()
        ctx = _context(registry, str(tmp_path), enabled)

        # Fresh session: an extended MCP tool is NOT visible up front.
        pre = {s["name"] for s in registry.schemas_for_adapter(enabled_extended=set())}
        assert "mcp_echo_echo" not in pre

        # tool_search surfaces it AND enables it for the rest of the session.
        search = await execute_tool(registry, "tool_search", {"query": "echo"}, ctx)
        assert search.is_error is False
        assert "mcp_echo_echo" in search.output
        assert "mcp_echo_echo" in enabled

        # Next turn: the enabled MCP tool is now in the adapter surface.
        visible = {
            s["name"] for s in registry.schemas_for_adapter(enabled_extended=enabled)
        }
        assert "mcp_echo_echo" in visible

        # And it actually executes against the live server.
        result = await execute_tool(registry, "mcp_echo_echo", {"text": "ping"}, ctx)
        assert result.is_error is False
        assert result.output == "ping"
    finally:
        await mgr.shutdown()


# ── 2. progressive disclosure holds with MCP tools present ──────────────────


async def test_session_start_stays_core_only_with_mcp_registered(
    tmp_path, monkeypatch
):
    """Against the LIVE boot registry: connecting an MCP server must not change
    the always-visible core surface — MCP tools are extended and only appear
    after an explicit `tool_search`."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setattr("tesseract.paths.TESSERACT_HOME", tmp_path)
    monkeypatch.setattr("tesseract.brain.boot.TESSERACT_HOME", tmp_path)

    from tesseract.brain.boot import build_tool_registry

    registry, *_ = build_tool_registry()
    core_before = {
        s["name"] for s in registry.schemas_for_adapter(enabled_extended=set())
    }

    mgr = await _connected_manager(registry, tmp_path)
    try:
        core_after = {
            s["name"] for s in registry.schemas_for_adapter(enabled_extended=set())
        }
        # The core surface is byte-for-byte unchanged...
        assert core_after == core_before
        # ...and no MCP tool leaked into it, even though they ARE registered.
        assert not {n for n in core_after if n.startswith("mcp_")}
        assert "mcp_echo_echo" in registry.tools
        assert registry.tools["mcp_echo_echo"].tier == "extended"
    finally:
        await mgr.shutdown()


# ── 3. representative capability query set ──────────────────────────────────


async def test_capability_queries_select_correct_tool(tmp_path, monkeypatch):
    """`tool_search` returns ONLY extended tools (core stays visible on its
    own), matches on name + description, and never surfaces an unrelated MCP
    tool for an off-topic query."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    registry = ToolRegistry()
    mgr = await _connected_manager(registry, tmp_path)
    try:
        async def _search(query: str) -> set[str]:
            enabled: set[str] = set()
            ctx = _context(registry, str(tmp_path), enabled)
            res = await execute_tool(registry, "tool_search", {"query": query}, ctx)
            assert res.is_error is False
            return enabled

        # Name-anchored query surfaces both echo tools.
        by_name = await _search("echo")
        assert {"mcp_echo_echo", "mcp_echo_raise_error"} <= by_name

        # Description-only recall: 'verbatim' appears only in echo's description,
        # not its name — proves matching reads the description, not just the name.
        by_desc = await _search("verbatim")
        assert "mcp_echo_echo" in by_desc
        assert "mcp_echo_raise_error" not in by_desc

        # Disambiguation within the MCP surface: an error-shaped query picks the
        # error tool, not the plain echo tool.
        errs = await _search("raise isError")
        assert "mcp_echo_raise_error" in errs
        assert "mcp_echo_echo" not in errs

        # Off-topic query surfaces NO MCP tool (no false-positive enablement).
        none = await _search("weather forecast tomorrow")
        assert not {n for n in none if n.startswith("mcp_echo_")}

        # tool_search never returns a CORE tool (they are already visible); the
        # returned set is a subset of the extended tier.
        extended_names = {
            n for n, t in registry.tools.items() if t.tier == "extended"
        }
        assert by_name <= extended_names
    finally:
        await mgr.shutdown()
