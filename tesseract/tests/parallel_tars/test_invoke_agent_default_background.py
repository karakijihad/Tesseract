"""parallel-tars P3 — invoke_agent backgrounds by default; registry-less
contexts degrade to foreground (pre-P3 the same call errored).

`_run_foreground` is monkeypatched — agent loading / adapter wiring is
out of scope here; the contract under test is the run() dispatch layer.
"""

from __future__ import annotations

import pytest

from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.invoke_agent import InvokeAgentInput, InvokeAgentTool


def _make_tool(monkeypatch: pytest.MonkeyPatch) -> InvokeAgentTool:
    tool = InvokeAgentTool.__new__(InvokeAgentTool)  # skip wiring-heavy __init__

    async def _fake_foreground(inp: InvokeAgentInput, context: ToolContext) -> ToolResult:
        return ToolResult(output=f"[{inp.name}] done", metadata={"agent": inp.name})

    monkeypatch.setattr(tool, "_run_foreground", _fake_foreground)
    return tool


def _make_ctx(registry: SpawnRegistry | None) -> ToolContext:
    ctx = ToolContext(workspace_root=".", session_id="parallel-tars-p3")
    ctx.spawns = registry
    return ctx


@pytest.mark.asyncio
async def test_default_background_returns_handle(monkeypatch):
    tool = _make_tool(monkeypatch)
    registry = SpawnRegistry()

    result = await tool.run(
        InvokeAgentInput(name="researcher", task="look it up"), _make_ctx(registry)
    )

    assert not result.is_error
    assert result.metadata["spawn_kind"] == "invoke_agent:researcher"
    handle = registry.get(result.metadata["spawn_handle"])
    inner = await handle.task
    assert inner.output == "[researcher] done"


@pytest.mark.asyncio
async def test_no_registry_degrades_to_foreground(monkeypatch):
    tool = _make_tool(monkeypatch)

    result = await tool.run(
        InvokeAgentInput(name="researcher", task="look it up"), _make_ctx(None)
    )

    assert not result.is_error
    assert "spawn_handle" not in (result.metadata or {})
    assert result.output == "[researcher] done"


@pytest.mark.asyncio
async def test_background_spawn_gets_detached_cancel_event(monkeypatch):
    """Reviewer finding 2026-07-09: the sub-agent's read-only tools raise
    CancelledError on context.cancel_event — a background sub-agent must get
    its own event, not the session-lifetime one any later Stop press sets."""
    tool = InvokeAgentTool.__new__(InvokeAgentTool)
    seen: dict[str, ToolContext] = {}

    async def _fake_foreground(inp: InvokeAgentInput, context: ToolContext) -> ToolResult:
        seen["context"] = context
        return ToolResult(output="done")

    monkeypatch.setattr(tool, "_run_foreground", _fake_foreground)
    registry = SpawnRegistry()
    ctx = _make_ctx(registry)

    result = await tool.run(InvokeAgentInput(name="researcher", task="dig"), ctx)
    handle = registry.get(result.metadata["spawn_handle"])
    await handle.task

    sub_ctx = seen["context"]
    assert sub_ctx.cancel_event is not ctx.cancel_event
    assert sub_ctx.session_id == ctx.session_id  # rest of the context carries over
    assert sub_ctx.spawns is registry
    ctx.cancel_event.set()  # a later turn's Stop button
    assert not sub_ctx.cancel_event.is_set()
    assert handle.cancel_fn is not None
    handle.cancel_fn()
    assert sub_ctx.cancel_event.is_set()


@pytest.mark.asyncio
async def test_background_false_runs_inline(monkeypatch):
    tool = _make_tool(monkeypatch)

    result = await tool.run(
        InvokeAgentInput(name="researcher", task="look it up", background=False),
        _make_ctx(SpawnRegistry()),
    )

    assert not result.is_error
    assert "spawn_handle" not in (result.metadata or {})
    assert result.output == "[researcher] done"
