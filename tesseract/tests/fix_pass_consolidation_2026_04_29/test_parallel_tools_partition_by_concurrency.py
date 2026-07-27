"""Audit M5 regression — `_run_pending_calls` must partition by
`is_concurrency_safe()`. Read-only tools run in parallel; mutating tools
run serially in original index order.

Before 2026-04-29 every tool was scheduled via `asyncio.create_task`
regardless of safety, so two `file_write` calls in the same model turn
could clobber each other and two `set_mood` calls could race the mood
state machine. The audit (M5) called this out as the parallel-safety
gap. This test wires fake tools through a real `ChatSession` and asserts
the recorded ordering proves serial execution of the unsafe slot.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from tesseract.brain.chat import ChatSession
from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)
from tesseract.kernel.state import ToolCall
from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)


class _Empty(BaseModel):
    pass


class _RecordingTool(Tool):
    """Tool that records when its run() starts + ends, lets us assert
    overlap (parallel) vs strict ordering (serial)."""

    def __init__(self, label: str, safe: bool, ledger: list[tuple[str, str]], delay: float) -> None:
        self._label = label
        self._safe = safe
        self._ledger = ledger
        self._delay = delay

    @property
    def name(self) -> str:
        return self._label

    @property
    def description(self) -> str:
        return f"recording tool {self._label}"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _Empty

    def is_concurrency_safe(self) -> bool:
        return self._safe

    def is_read_only(self) -> bool:
        return self._safe

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.ALLOW

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        self._ledger.append(("start", self._label))
        await asyncio.sleep(self._delay)
        self._ledger.append(("end", self._label))
        return ToolResult(output=f"{self._label}-done")


class _OneShotAdapter(ModelAdapter):
    """Emits two TOOL_CALL_END chunks then STOP. Operator turn drives
    `_run_pending_calls` once over both calls."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls
        self._sent = False

    @property
    def model(self) -> str:
        return "test-adapter"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ):
        if not self._sent:
            self._sent = True
            for c in self._calls:
                yield StreamChunk(
                    type=ChunkType.TOOL_CALL_END,
                    tool_call_id=c["id"],
                    tool_call=ToolCall(id=c["id"], name=c["name"], input=c.get("input", {})),
                )
            yield StreamChunk(type=ChunkType.STOP, stop_reason="tool_use")
        else:
            yield StreamChunk(type=ChunkType.TEXT, text="done")
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")

    def count_tokens(self, messages):
        return 0

    async def check_available(self):
        return True


def _drive_one_turn(session: ChatSession, prompt: str) -> None:
    async def _run():
        async for _ in session.send(prompt):
            pass
    asyncio.run(_run())


@pytest.mark.parametrize("safe_first", [True, False])
def test_safe_tools_run_in_parallel_unsafe_serial(safe_first: bool) -> None:
    ledger: list[tuple[str, str]] = []

    safe1 = _RecordingTool("safe_a", safe=True, ledger=ledger, delay=0.05)
    safe2 = _RecordingTool("safe_b", safe=True, ledger=ledger, delay=0.05)
    unsafe = _RecordingTool("unsafe_x", safe=False, ledger=ledger, delay=0.02)

    registry = ToolRegistry()
    for t in (safe1, safe2, unsafe):
        registry.register(t)

    pending = [
        {"id": "c1", "name": "safe_a"},
        {"id": "c2", "name": "unsafe_x"},
        {"id": "c3", "name": "safe_b"},
    ]
    if not safe_first:
        pending = list(reversed(pending))

    adapter = _OneShotAdapter(pending)
    session = ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        prompt_builder=lambda: "",
        options=AdapterOptions(model="test", provider="test"),
        registry=registry,
        tool_context=ToolContext(workspace_root="/"),
    )

    _drive_one_turn(session, "go")

    # Both safe tools' [start, end] windows must overlap (both started
    # before either ended).
    safe_starts = [i for i, ev in enumerate(ledger) if ev[0] == "start" and ev[1].startswith("safe")]
    safe_ends = [i for i, ev in enumerate(ledger) if ev[0] == "end" and ev[1].startswith("safe")]
    assert len(safe_starts) == 2 and len(safe_ends) == 2
    assert max(safe_starts) < min(safe_ends), (
        f"safe tools did not overlap (started serially): {ledger}"
    )

    # Unsafe tool must have started AFTER all safe tools ended (not
    # interleaved). The implementation runs safe-as-completed first,
    # then unsafe serially.
    unsafe_start = next(i for i, ev in enumerate(ledger) if ev == ("start", "unsafe_x"))
    assert unsafe_start > max(safe_ends), (
        f"unsafe tool started before all safe finished: {ledger}"
    )


def test_two_unsafe_tools_run_serial_in_pending_order() -> None:
    """Two unsafe tools in one model turn must execute end-to-end in
    pending_calls order — no overlap, no reordering."""
    ledger: list[tuple[str, str]] = []
    a = _RecordingTool("unsafe_first", safe=False, ledger=ledger, delay=0.04)
    b = _RecordingTool("unsafe_second", safe=False, ledger=ledger, delay=0.01)

    registry = ToolRegistry()
    registry.register(a)
    registry.register(b)

    adapter = _OneShotAdapter([
        {"id": "c1", "name": "unsafe_first"},
        {"id": "c2", "name": "unsafe_second"},
    ])
    session = ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        prompt_builder=lambda: "",
        options=AdapterOptions(model="test", provider="test"),
        registry=registry,
        tool_context=ToolContext(workspace_root="/"),
    )
    _drive_one_turn(session, "go")

    assert ledger == [
        ("start", "unsafe_first"),
        ("end", "unsafe_first"),
        ("start", "unsafe_second"),
        ("end", "unsafe_second"),
    ], f"unsafe tools did not run strictly serial in order: {ledger}"


def test_history_append_order_matches_pending_calls() -> None:
    """Regression — even when safe tools complete out of order, the
    history append order must follow pending_calls so downstream adapter
    calls see a deterministic message sequence."""
    ledger: list[tuple[str, str]] = []
    fast = _RecordingTool("safe_fast", safe=True, ledger=ledger, delay=0.001)
    slow = _RecordingTool("safe_slow", safe=True, ledger=ledger, delay=0.05)

    registry = ToolRegistry()
    registry.register(fast)
    registry.register(slow)

    adapter = _OneShotAdapter([
        {"id": "c-slow", "name": "safe_slow"},
        {"id": "c-fast", "name": "safe_fast"},
    ])
    session = ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        prompt_builder=lambda: "",
        options=AdapterOptions(model="test", provider="test"),
        registry=registry,
        tool_context=ToolContext(workspace_root="/"),
    )
    _drive_one_turn(session, "go")

    tool_msgs = [m for m in session.history if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c-slow", "c-fast"]
