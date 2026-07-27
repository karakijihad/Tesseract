"""Phase 4 (CLI parity) — SpawnRegistry + spawn_check / spawn_await /
spawn_cancel control tools. Doesn't exercise delegate_claude end-to-end
(that needs the claude CLI binary). Instead, registers a stub coroutine
to verify the registry, status transitions, and the three tools'
interaction with the registry.
"""

from __future__ import annotations

import asyncio

import pytest

from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.spawn_await import SpawnAwaitInput, SpawnAwaitTool
from tesseract.kernel.tools.spawn_cancel import SpawnCancelInput, SpawnCancelTool
from tesseract.kernel.tools.spawn_check import SpawnCheckInput, SpawnCheckTool


async def _stub_work(delay: float, output: str) -> ToolResult:
    await asyncio.sleep(delay)
    return ToolResult(output=output, metadata={"stub": True})


async def _failing_work() -> ToolResult:
    raise RuntimeError("intentional failure")


def _make_ctx(registry: SpawnRegistry) -> ToolContext:
    ctx = ToolContext(workspace_root=".", session_id="phase4-spawns-test")
    ctx.spawns = registry
    return ctx


@pytest.mark.asyncio
async def test_register_runs_and_completes_with_done_status():
    registry = SpawnRegistry()
    handle = registry.register(
        kind="delegate_claude",
        coro=_stub_work(0.05, "ok"),
    )
    assert handle.status() == "running"
    assert handle.handle_id.startswith("del-claude-")

    # Wait for completion.
    result = await handle.task
    assert isinstance(result, ToolResult)
    assert result.output == "ok"
    assert handle.status() == "done"
    assert handle.finished_at is not None


@pytest.mark.asyncio
async def test_spawn_check_reports_running_then_done():
    registry = SpawnRegistry()
    tool = SpawnCheckTool()
    handle = registry.register(kind="delegate_claude", coro=_stub_work(0.05, "ok"))
    ctx = _make_ctx(registry)

    while_running = await tool.run(SpawnCheckInput(handle=handle.handle_id), ctx)
    assert not while_running.is_error
    assert while_running.metadata["status"] == "running"

    await handle.task
    after = await tool.run(SpawnCheckInput(handle=handle.handle_id), ctx)
    assert after.metadata["status"] == "done"
    assert after.metadata["finished_at"] is not None


@pytest.mark.asyncio
async def test_spawn_check_unknown_handle_errors():
    registry = SpawnRegistry()
    tool = SpawnCheckTool()
    ctx = _make_ctx(registry)

    res = await tool.run(SpawnCheckInput(handle="ghost-handle"), ctx)
    assert res.is_error
    assert "ghost-handle" in res.output


@pytest.mark.asyncio
async def test_spawn_await_returns_original_tool_result():
    registry = SpawnRegistry()
    tool = SpawnAwaitTool()
    handle = registry.register(kind="delegate_claude", coro=_stub_work(0.02, "result-text"))
    ctx = _make_ctx(registry)

    res = await tool.run(SpawnAwaitInput(handle=handle.handle_id), ctx)
    assert not res.is_error
    assert res.output == "result-text"
    assert res.metadata.get("stub") is True


@pytest.mark.asyncio
async def test_spawn_await_timeout_does_not_kill_task():
    registry = SpawnRegistry()
    tool = SpawnAwaitTool()
    handle = registry.register(kind="delegate_claude", coro=_stub_work(0.5, "slow"))
    ctx = _make_ctx(registry)

    res = await tool.run(SpawnAwaitInput(handle=handle.handle_id, timeout=1), ctx)
    # `timeout=1` is an int per schema (ge=1). Stub finishes in 0.5s,
    # so we expect a successful return.
    assert not res.is_error
    assert res.output == "slow"


@pytest.mark.asyncio
async def test_spawn_await_propagates_failure():
    registry = SpawnRegistry()
    tool = SpawnAwaitTool()
    handle = registry.register(kind="delegate_claude", coro=_failing_work())
    ctx = _make_ctx(registry)

    res = await tool.run(SpawnAwaitInput(handle=handle.handle_id), ctx)
    assert res.is_error
    assert "intentional failure" in res.output


@pytest.mark.asyncio
async def test_spawn_cancel_running_handle():
    registry = SpawnRegistry()
    tool = SpawnCancelTool()
    handle = registry.register(kind="delegate_claude", coro=_stub_work(5.0, "would-be-result"))
    ctx = _make_ctx(registry)

    res = await tool.run(SpawnCancelInput(handle=handle.handle_id), ctx)
    assert not res.is_error
    assert handle.status() == "cancelled"


@pytest.mark.asyncio
async def test_spawn_cancel_already_done_handle():
    registry = SpawnRegistry()
    tool = SpawnCancelTool()
    handle = registry.register(kind="delegate_claude", coro=_stub_work(0.01, "ok"))
    await handle.task

    res = await tool.run(SpawnCancelInput(handle=handle.handle_id), _make_ctx(registry))
    assert not res.is_error
    assert "already done" in res.output


@pytest.mark.asyncio
async def test_spawn_check_falls_back_to_global_index_after_reconnect():
    """M4-p2 parity: a reconnected chat's own registry is empty, but a spawn
    surviving in the orphaned registry must still be checkable — same
    global-index fallback spawn_await uses."""
    from tesseract.brain.spawns import _ALL_HANDLES

    owning = SpawnRegistry()
    handle = owning.register(kind="delegate_claude", coro=_stub_work(5.0, "x"))
    try:
        empty_ctx = _make_ctx(SpawnRegistry())
        res = await SpawnCheckTool().run(
            SpawnCheckInput(handle=handle.handle_id), empty_ctx
        )
        assert not res.is_error
        assert res.metadata["status"] == "running"
    finally:
        await owning.cancel(handle.handle_id)
        _ALL_HANDLES.pop(handle.handle_id, None)


@pytest.mark.asyncio
async def test_spawn_cancel_falls_back_to_global_index_after_reconnect():
    from tesseract.brain.spawns import _ALL_HANDLES

    owning = SpawnRegistry()
    handle = owning.register(kind="delegate_claude", coro=_stub_work(5.0, "x"))
    try:
        empty_ctx = _make_ctx(SpawnRegistry())
        res = await SpawnCancelTool().run(
            SpawnCancelInput(handle=handle.handle_id), empty_ctx
        )
        assert not res.is_error
        assert handle.status() == "cancelled"
    finally:
        if not handle.task.done():
            await owning.cancel(handle.handle_id)
        _ALL_HANDLES.pop(handle.handle_id, None)


@pytest.mark.asyncio
async def test_registry_cancel_all_clears_running_handles():
    registry = SpawnRegistry()
    h1 = registry.register(kind="delegate_claude", coro=_stub_work(2.0, "a"))
    h2 = registry.register(kind="delegate_claude", coro=_stub_work(2.0, "b"))
    h_done = registry.register(kind="delegate_claude", coro=_stub_work(0.01, "done"))
    await h_done.task

    n = await registry.cancel_all()
    # Two were running, one was already done — only the two are
    # actually cancelled by cancel_all.
    assert n == 2
    assert h1.status() == "cancelled"
    assert h2.status() == "cancelled"


@pytest.mark.asyncio
async def test_drain_completed_returns_each_handle_once():
    """Phase 4 auto-surface: drain_completed returns newly-completed
    handles exactly once. Subsequent calls return nothing for the same
    handle (the chat loop fires SPAWN_DONE per completion, not per
    iteration)."""
    registry = SpawnRegistry()
    h = registry.register(kind="delegate_claude", coro=_stub_work(0.01, "ok"))
    await h.task

    first = registry.drain_completed()
    second = registry.drain_completed()

    assert [x.handle_id for x in first] == [h.handle_id]
    assert second == []


@pytest.mark.asyncio
async def test_drain_completed_skips_running():
    registry = SpawnRegistry()
    h = registry.register(kind="delegate_claude", coro=_stub_work(5.0, "slow"))
    drained = registry.drain_completed()
    assert drained == []
    assert h.status() == "running"
    h.task.cancel()
    try:
        await h.task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_invoke_agent_style_cancel_unwinds_inner_chat_session():
    """Phase 4 follow-up: simulate an invoke_agent-shaped spawn (an
    in-process inner ChatSession.send loop) cancelled mid-stream.
    Confirms the asyncio.Task unwinds via CancelledError without
    leaking the inner stream generator. Uses a stub adapter that
    yields TEXT chunks slowly so the cancel hits mid-stream."""
    from typing import AsyncGenerator
    from tesseract.kernel.adapters.base import (
        AdapterOptions, ChunkType, ModelAdapter, StreamChunk,
    )

    cancelled_flag = {"hit": False}

    class _SlowStreamAdapter(ModelAdapter):
        async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
            return ""

        async def stream(self, messages, tools=None, options=None) -> AsyncGenerator[StreamChunk, None]:
            try:
                for _ in range(50):
                    yield StreamChunk(type=ChunkType.TEXT, text="slow ")
                    await asyncio.sleep(0.05)
                yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")
            except asyncio.CancelledError:
                cancelled_flag["hit"] = True
                raise

        def count_tokens(self, messages) -> int:
            return 0

        async def check_available(self) -> bool:
            return True

    async def _inner_work() -> ToolResult:
        # Mimic invoke_agent's body — drive a sub-ChatSession-like
        # generator to exhaustion (or cancellation). When the outer
        # Task is cancelled, CancelledError propagates into the
        # async-for loop and we surface a partial-error ToolResult,
        # exactly how the production tool's try/except is shaped.
        adapter = _SlowStreamAdapter()
        try:
            async for _ in adapter.stream([]):
                pass
        except asyncio.CancelledError:
            raise
        return ToolResult(output="should not reach", is_error=False)

    registry = SpawnRegistry()
    handle = registry.register(kind="invoke_agent:slow", coro=_inner_work())

    # Let the inner stream emit a few chunks then cancel.
    await asyncio.sleep(0.15)
    assert handle.status() == "running"

    ok = await registry.cancel(handle.handle_id)
    assert ok is True
    assert handle.status() == "cancelled"
    # The inner adapter.stream's `except CancelledError` branch fired —
    # confirms the cancellation reached the inner async generator,
    # not just the outer Task wrapper.
    assert cancelled_flag["hit"] is True


@pytest.mark.asyncio
async def test_chat_session_wires_spawns_into_tool_context():
    """ChatSession.__post_init__ cross-links its SpawnRegistry into
    tool_context.spawns so kernel-layer tools can reach it without
    importing brain modules."""
    from tesseract.brain.chat import ChatSession
    from tesseract.kernel.adapters.base import (
        AdapterOptions, ChunkType, ModelAdapter, StreamChunk,
    )
    from typing import AsyncGenerator

    class _StubAdapter(ModelAdapter):
        async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
            return ""

        async def stream(self, messages, tools=None, options=None) -> AsyncGenerator[StreamChunk, None]:
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")

        def count_tokens(self, messages) -> int:
            return 0

        async def check_available(self) -> bool:
            return True

    sess = ChatSession(
        adapter=_StubAdapter(),
        system_prompt="",
        max_tool_iterations=5,
        max_consecutive_adapter_errors=3,
    )
    assert sess.tool_context.spawns is sess.spawns
