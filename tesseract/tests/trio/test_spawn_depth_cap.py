"""W3 — spawn nesting-depth cap (`runtime.yaml::max_spawn_depth`).

Registry-level: register at/past the depth raises SpawnDepthExceeded (a
SpawnCapExceeded subclass so existing call sites handle it). Tool-result
mapping renders the "don't nest deeper" guidance. Context threading: the
sub-agent factory bumps the copied context's depth; ChatSession stamps
both fields onto its registry. Loader raises loudly on a missing key.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tesseract.brain.spawns import SpawnRegistry
from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_max_spawn_depth,
)
from tesseract.kernel.tools.base import (
    SpawnCapExceeded,
    SpawnDepthExceeded,
    ToolContext,
    ToolResult,
    spawn_cap_tool_result,
)


async def _quick() -> ToolResult:
    return ToolResult(output="ok")


@pytest.mark.asyncio
async def test_register_at_depth_cap_raises():
    registry = SpawnRegistry()
    registry.depth = 3
    registry.max_depth = 3
    with pytest.raises(SpawnDepthExceeded) as exc_info:
        registry.register(kind="delegate_claude", coro=_quick())
    assert exc_info.value.depth == 3
    assert exc_info.value.cap == 3


@pytest.mark.asyncio
async def test_register_below_depth_cap_succeeds():
    registry = SpawnRegistry()
    registry.depth = 2
    registry.max_depth = 3
    handle = registry.register(kind="delegate_claude", coro=_quick())
    await handle.task
    assert handle.status() == "done"


@pytest.mark.asyncio
async def test_uncapped_depth_unaffected():
    registry = SpawnRegistry()
    registry.depth = 99  # no max_depth set
    handle = registry.register(kind="delegate_claude", coro=_quick())
    await handle.task
    assert handle.status() == "done"


def test_depth_exceeded_is_a_cap_exceeded():
    """Existing call sites catch SpawnCapExceeded only — the depth error
    must flow through them unchanged."""
    exc = SpawnDepthExceeded(3, 3)
    assert isinstance(exc, SpawnCapExceeded)


def test_tool_result_renders_depth_guidance():
    result = spawn_cap_tool_result(SpawnDepthExceeded(3, 3))
    assert result.is_error
    assert result.metadata["reason"] == "spawn_depth_exceeded"
    assert "not spawn deeper" in result.output
    # The concurrency message is unchanged.
    conc = spawn_cap_tool_result(SpawnCapExceeded(8, 8))
    assert conc.metadata["reason"] == "spawn_cap_exceeded"


def test_chat_session_stamps_registry_from_context():
    from tesseract.brain.chat import ChatSession

    ctx = ToolContext(spawn_depth=2, spawn_depth_cap=5)
    session = ChatSession(
        adapter=object(),
        system_prompt="",
        max_tool_iterations=5,
        max_consecutive_adapter_errors=3,
        tool_context=ctx,
    )
    assert session.spawns.depth == 2
    assert session.spawns.max_depth == 5


def test_agent_factory_bumps_depth(monkeypatch, tmp_path: Path):
    """A sub-agent session is one nesting level deeper than its parent; the
    cap rides along on the copied context; the parent context is untouched."""
    from tesseract.brain import agent_factory
    from tesseract.kernel.tools import invoke_agent as ia

    class _Agent:
        disabled = False
        model_role = "agents_default"
        name = "helper"

    class _Reg:
        def names(self):
            return []

    monkeypatch.setattr(
        agent_factory, "load_agent", lambda name, agents_dir: _Agent()
    )
    monkeypatch.setattr(ia, "_build_sub_registry", lambda parent, agent: _Reg())
    monkeypatch.setattr(ia, "_compose_sub_system_prompt", lambda agent, names: "sub")
    monkeypatch.setattr(ia, "_resolve_sub_adapter", lambda pa, po, agent: (pa, po))
    monkeypatch.setattr(ia, "_is_cli_role", lambda role: False)

    parent_ctx = ToolContext(spawn_depth=1, spawn_depth_cap=3)
    session = agent_factory.build_agent_session(
        name="helper",
        agents_dir=tmp_path,
        parent_adapter=object(),
        parent_options=None,
        parent_registry=None,
        max_tool_iterations=5,
        max_consecutive_adapter_errors=3,
        tool_context=parent_ctx,
        policy=None,
        ask_fn=None,
    )
    assert session.spawns.depth == 2
    assert session.spawns.max_depth == 3
    assert parent_ctx.spawn_depth == 1  # parent context untouched


def test_loader_reads_repo_config_and_rejects_missing_key(tmp_path: Path):
    assert load_max_spawn_depth(default_runtime_config_path()) >= 1
    stub = tmp_path / "runtime.yaml"
    stub.write_text("spawn_stall_seconds: 900\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_spawn_depth"):
        load_max_spawn_depth(stub)
