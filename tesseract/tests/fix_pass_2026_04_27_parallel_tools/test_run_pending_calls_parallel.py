"""Parallel tool execution — match Claude Code's contract: when the
model emits N tool_use blocks in one response, run them concurrently.

Verifies:
- Two tools run overlapping in time, not serialized
- Each tool's `current_call_id` is its own (no race on shared
  ToolContext field)
- TOOL_RESULT stream chunks fire as tools finish (operator sees the
  fast tool first), but history is appended in pending_calls order
- `as_completed` ordering does not corrupt history
- CancelledError mid-flight cancels remaining tasks and writes
  placeholder rows for unfinished ones in deterministic order
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from pydantic import BaseModel

from tesseract.brain.chat import ChatSession
from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType
from tesseract.kernel.state import ToolCall
from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)


class _Schema(BaseModel):
    pass


class _SlowTool(Tool):
    """Sleeps `delay` seconds then returns a fingerprint that includes
    the call_id it observed. Lets the test verify per-call context
    isolation under parallel execution."""

    def __init__(self, name: str, delay: float) -> None:
        self._name = name
        self._delay = delay
        self.observed_call_ids: list[str] = []
        self.start_times: list[float] = []
        self.end_times: list[float] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"slow tool {self._name}"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _Schema

    def is_concurrency_safe(self) -> bool:
        # Audit M5 partition (2026-04-29): _run_pending_calls now schedules
        # only `is_concurrency_safe()=True` tools via asyncio.create_task.
        # These tests verify the parallel slot, so the fixture opts in.
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        self.start_times.append(time.monotonic())
        self.observed_call_ids.append(context.current_call_id)
        await asyncio.sleep(self._delay)
        self.end_times.append(time.monotonic())
        return ToolResult(output=f"{self._name}:{context.current_call_id}")


def _build_session(registry: ToolRegistry) -> ChatSession:
    """Build a minimal `ChatSession` for the test. We only exercise
    `_run_pending_calls`, so the adapter / system prompt / cost ledger
    aren't touched."""
    from unittest.mock import MagicMock

    return ChatSession(
        adapter=MagicMock(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        prompt_builder=None,
        options=AdapterOptions(model="x", context_window=8000),
        registry=registry,
        tool_context=ToolContext(workspace_root=".", session_id="test"),
        compact_threshold=0.4,
        keep_recent_turns=10,
        ask_fn=None,
        policy=None,
        cost_ledger=None,
    )


async def _drain(gen) -> list[Any]:
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


async def test_two_tools_run_in_parallel_not_serialized() -> None:
    """If tool A sleeps 200ms and tool B sleeps 200ms, total wall-clock
    must be ~200ms, not ~400ms (serialized). Generous bound for CI
    flakiness."""
    a = _SlowTool("alpha", delay=0.2)
    b = _SlowTool("beta", delay=0.2)
    registry = ToolRegistry()
    registry.register(a)
    registry.register(b)
    sess = _build_session(registry)

    pending = [
        ToolCall(id="ca1", name="alpha", input={}),
        ToolCall(id="cb1", name="beta", input={}),
    ]

    t0 = time.monotonic()
    chunks = await _drain(sess._run_pending_calls(pending))
    elapsed = time.monotonic() - t0

    # Both tools ran (registry called once each, completed).
    assert len(a.start_times) == 1
    assert len(b.start_times) == 1
    # Wall-clock < 1.5x single-tool latency proves parallelism.
    assert elapsed < 0.35, f"expected parallel ~0.2s, got {elapsed:.3f}s"
    # Both TOOL_RESULT chunks emitted.
    results = [c for c in chunks if c.type is ChunkType.TOOL_RESULT]
    assert len(results) == 2


async def test_each_tool_sees_its_own_call_id() -> None:
    """The shared `ToolContext.current_call_id` field must NOT race
    across parallel tasks. Each tool reads the call_id of its OWN
    tool_use block, not whichever was set last by another task."""
    a = _SlowTool("alpha", delay=0.05)
    b = _SlowTool("beta", delay=0.05)
    registry = ToolRegistry()
    registry.register(a)
    registry.register(b)
    sess = _build_session(registry)

    pending = [
        ToolCall(id="call-A", name="alpha", input={}),
        ToolCall(id="call-B", name="beta", input={}),
    ]
    await _drain(sess._run_pending_calls(pending))

    assert a.observed_call_ids == ["call-A"], (
        f"tool alpha must see its own id; got {a.observed_call_ids}"
    )
    assert b.observed_call_ids == ["call-B"], (
        f"tool beta must see its own id; got {b.observed_call_ids}"
    )


async def test_history_appended_in_pending_call_order_not_completion_order() -> None:
    """Tool B finishes first (shorter delay), but history must list A
    before B because that's the order chat_brain emitted them. Stable
    history shape matters: the next adapter call must see a
    deterministic message sequence regardless of completion timing."""
    a = _SlowTool("alpha", delay=0.15)
    b = _SlowTool("beta", delay=0.02)
    registry = ToolRegistry()
    registry.register(a)
    registry.register(b)
    sess = _build_session(registry)

    pending = [
        ToolCall(id="ca", name="alpha", input={}),
        ToolCall(id="cb", name="beta", input={}),
    ]
    await _drain(sess._run_pending_calls(pending))

    tool_msgs = [m for m in sess.history if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "ca", (
        f"alpha must be first in history; got {[m['tool_call_id'] for m in tool_msgs]}"
    )
    assert tool_msgs[1]["tool_call_id"] == "cb"


async def test_stream_yields_tool_result_as_each_completes() -> None:
    """The streaming UX value of parallel execution: TOOL_RESULT chunks
    fire as tools finish, NOT held until everything is done. Tool B
    (faster) should yield before tool A (slower)."""
    a = _SlowTool("alpha", delay=0.15)
    b = _SlowTool("beta", delay=0.02)
    registry = ToolRegistry()
    registry.register(a)
    registry.register(b)
    sess = _build_session(registry)

    pending = [
        ToolCall(id="ca", name="alpha", input={}),
        ToolCall(id="cb", name="beta", input={}),
    ]
    chunks = await _drain(sess._run_pending_calls(pending))
    result_chunks = [c for c in chunks if c.type is ChunkType.TOOL_RESULT]
    assert len(result_chunks) == 2
    # First yielded chunk is the faster tool (beta), proving
    # streaming-as-completed semantics.
    assert result_chunks[0].tool_call_id == "cb"
    assert result_chunks[1].tool_call_id == "ca"


async def test_cancellation_writes_placeholders_in_pending_call_order() -> None:
    """Operator hits cancel mid-batch. All in-flight tasks cancel; the
    history must list every pending call (in the original order) with
    either the real result or `[cancelled by operator]` placeholder so
    the next adapter call doesn't see orphaned tool_use without a
    matching tool_result."""
    a = _SlowTool("alpha", delay=10.0)  # never finishes within test
    b = _SlowTool("beta", delay=10.0)
    registry = ToolRegistry()
    registry.register(a)
    registry.register(b)
    sess = _build_session(registry)

    pending = [
        ToolCall(id="ca", name="alpha", input={}),
        ToolCall(id="cb", name="beta", input={}),
    ]

    async def _runner() -> list[Any]:
        return await _drain(sess._run_pending_calls(pending))

    task = asyncio.create_task(_runner())
    # Yield until both tools have started, then cancel.
    while len(a.start_times) < 1 or len(b.start_times) < 1:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    tool_msgs = [m for m in sess.history if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "ca"
    assert tool_msgs[1]["tool_call_id"] == "cb"
    assert "cancelled" in tool_msgs[0]["content"].lower()
    assert "cancelled" in tool_msgs[1]["content"].lower()
