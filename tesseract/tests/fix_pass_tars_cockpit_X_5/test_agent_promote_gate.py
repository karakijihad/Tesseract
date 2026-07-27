"""X-5 Session B — agent_promote Layer-A gate (CL-3).

Tests the operator-gate contract for `AgentPromoteTool`:
- `check_permissions` ALWAYS returns `PermissionResult.ASK` for kind=agent —
  this is the Layer-A guarantee (cannot be auto-overridden by posture).
- `check_permissions` returns `PermissionResult.DENY` for kind=tool (short-
  circuit so `run` surfaces the human-readable redirect error).
- Full promote round-trip under isolated home: pending → active.
- `default_posture` is "ask" — recorded on the class, not overridable to "auto".

Here we verify the gate contract from the X-5 / CL-3 perspective (i.e. the
ASK-always guarantee that makes agent_promote safe even when an autonomy
posture override has granted `auto` for other tools)."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from tesseract.kernel.tools.agent_promote import AgentPromoteInput, AgentPromoteTool
from tesseract.kernel.tools.base import PermissionResult, ToolContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AGENT_MD = textwrap.dedent("""\
    ---
    name: test-sentinel
    version: "0.1"
    model_role: agents_default
    description: A synthetic agent for gate tests.
    ---

    ## Role

    Sentinel agent for X-5 Session B gate tests.
""")


def _make_tool(agents_dir: Path) -> AgentPromoteTool:
    return AgentPromoteTool(agents_dir=agents_dir)


def _ctx(isolated_home: Path) -> ToolContext:
    return ToolContext(workspace_root=str(isolated_home))


def _plant_pending(agents_dir: Path, name: str = "test-sentinel") -> None:
    """Write a minimal pending agent .md so list_pending_agents / load_agent work."""
    pending_dir = agents_dir / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / f"{name}.md").write_text(_AGENT_MD, encoding="utf-8")


# ---------------------------------------------------------------------------
# Gate: check_permissions always returns ASK for kind=agent
# ---------------------------------------------------------------------------


def test_check_permissions_kind_agent_always_ask(isolated_home: Path) -> None:
    """Layer-A gate: kind=agent MUST return PermissionResult.ASK regardless of
    any other context — posture overrides cannot auto-allow agent promotion."""
    agents_dir = isolated_home / "agents"
    agents_dir.mkdir()
    tool = _make_tool(agents_dir)
    inp = AgentPromoteInput(name="any-name", kind="agent")
    ctx = _ctx(isolated_home)
    result = tool.check_permissions(inp, ctx)
    assert result is PermissionResult.ASK


def test_check_permissions_kind_tool_returns_deny(isolated_home: Path) -> None:
    """kind=tool short-circuits to DENY so no operator prompt is shown for an
    immediately-rejected request."""
    agents_dir = isolated_home / "agents"
    agents_dir.mkdir()
    tool = _make_tool(agents_dir)
    inp = AgentPromoteInput(name="any-name", kind="tool")
    ctx = _ctx(isolated_home)
    result = tool.check_permissions(inp, ctx)
    assert result is PermissionResult.DENY


def test_default_posture_is_ask() -> None:
    """The class-level default_posture must be 'ask' — this is what the
    permissions engine reads when no explicit posture override is in effect."""
    assert AgentPromoteTool.default_posture == "ask"


# ---------------------------------------------------------------------------
# Gate: kind=tool run() returns an error redirect, not a partial execution
# ---------------------------------------------------------------------------


def test_run_kind_tool_returns_error_without_side_effects(isolated_home: Path) -> None:
    """Even if check_permissions is somehow bypassed, run() with kind=tool
    returns an error — no files are moved."""
    agents_dir = isolated_home / "agents"
    agents_dir.mkdir()
    _plant_pending(agents_dir)
    tool = _make_tool(agents_dir)
    inp = AgentPromoteInput(name="test-sentinel", kind="tool")
    ctx = _ctx(isolated_home)
    result = asyncio.run(tool.run(inp, ctx))
    assert result.is_error
    assert "does not handle tool promotions" in result.output
    # File was NOT moved — still pending.
    assert (agents_dir / "pending" / "test-sentinel.md").exists()


# ---------------------------------------------------------------------------
# Happy path: full promote round-trip under isolated home
# ---------------------------------------------------------------------------


def test_promote_kind_agent_moves_file(isolated_home: Path) -> None:
    """Operator approves → file moves from pending/ to agents/."""
    agents_dir = isolated_home / "agents"
    agents_dir.mkdir()
    _plant_pending(agents_dir)
    tool = _make_tool(agents_dir)
    inp = AgentPromoteInput(name="test-sentinel", kind="agent")
    ctx = _ctx(isolated_home)
    result = asyncio.run(tool.run(inp, ctx))
    assert not result.is_error, result.output
    # File is active.
    assert (agents_dir / "test-sentinel.md").exists()
    # Pending copy is gone.
    assert not (agents_dir / "pending" / "test-sentinel.md").exists()


def test_promote_appends_index_row(isolated_home: Path) -> None:
    """After promotion the agent slug appears in INDEX.md."""
    agents_dir = isolated_home / "agents"
    agents_dir.mkdir()
    _plant_pending(agents_dir)
    tool = _make_tool(agents_dir)
    ctx = _ctx(isolated_home)
    asyncio.run(tool.run(AgentPromoteInput(name="test-sentinel"), ctx))
    index_text = (agents_dir / "INDEX.md").read_text(encoding="utf-8")
    assert "test-sentinel" in index_text


def test_promote_missing_agent_returns_error(isolated_home: Path) -> None:
    """Promoting a name that isn't in pending/ returns a clean error — no crash."""
    agents_dir = isolated_home / "agents"
    agents_dir.mkdir()
    (agents_dir / "pending").mkdir()
    tool = _make_tool(agents_dir)
    ctx = _ctx(isolated_home)
    result = asyncio.run(tool.run(AgentPromoteInput(name="ghost-agent"), ctx))
    assert result.is_error
    assert "ghost-agent" in result.output
