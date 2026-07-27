"""Owner batch 1 follow-up — `_run_pending_calls` must always leave
history with a `role: tool` message for every emitted `tool_call`,
regardless of how the generator exits.

Prior version only handled `asyncio.CancelledError`. A non-cancel
exception (tool registry crash, downstream raise, etc.) would propagate
out of the generator with the assistant message + tool_calls already in
history but ZERO matching tool messages — a permanent function_call/
output pairing violation that bricked every subsequent chat turn with
`OpenAI 400 — No tool output found for function call call_...`. Owner
caught this 2026-04-29: a turn with 3 `memory_save` calls left orphans
in the session file and every subsequent chat turn fell back to Gemini.

The fix replaces the narrow `except CancelledError` with a `finally`
block that ensures every pending_call has a placeholder before the
generator returns, on all three exit paths: success, cancel, and
arbitrary exception.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.kernel.state import ToolCall
from tesseract.kernel.tools.base import ToolContext


@dataclass
class _StubResult:
    output: str
    is_error: bool = False
    denied_hard: bool = False
    deny_reason: str = ""
    # Added 2026-05-10 to match the ToolResult shape after the
    # workspace-detail enrichment work (edfdd01) — `_result_chunk`
    # now copies `result.metadata` onto the TOOL_RESULT chunk's raw,
    # so the fixture must expose the field (None when not set).
    metadata: dict | None = None


def _make_session(registry: Any) -> ChatSession:
    """Build a barebones ChatSession with just enough wiring for
    `_run_pending_calls` to be exercisable. No adapter/network calls."""
    sess = ChatSession.__new__(ChatSession)
    sess.history = []
    sess.registry = registry
    sess.tool_context = ToolContext(workspace_root=".", session_id="test")
    sess.ask_fn = None
    sess.policy = None
    sess.options = AdapterOptions(role="chat_brain", model="stub", provider="stub")
    sess.adapter = MagicMock()
    sess.cost_ledger = None
    sess._observer_last_index = 0
    sess._pending_suggestions = []
    sess._pending_conscience = []
    sess._observed_ids = set()
    sess._turn_injection = ""
    sess._tool_error_streak_name = ""
    sess._tool_error_streak_count = 0
    sess._failures_scope_id = "test"
    return sess


async def _drain(gen):
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_pending_calls_history_has_tool_message_per_call_on_success(monkeypatch):
    """Baseline: 3 calls all succeed, history gets 3 tool messages in order."""
    registry = MagicMock()
    registry.get.return_value = MagicMock(is_concurrency_safe=lambda: False)
    sess = _make_session(registry)

    async def fake_execute(**kwargs):
        return _StubResult(output=f"ok:{kwargs['tool_name']}")

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", fake_execute)

    pending = [
        ToolCall(id="c1", name="memory_save", input={}),
        ToolCall(id="c2", name="memory_save", input={}),
        ToolCall(id="c3", name="memory_save", input={}),
    ]
    await _drain(sess._run_pending_calls(pending))

    tool_msgs = [m for m in sess.history if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_pending_calls_history_has_placeholders_on_non_cancel_exception(monkeypatch):
    """Regression: when execute_tool raises a non-CancelledError, every
    unfinished call must still get a `role: tool` placeholder. Without the
    finally-block fix, the assistant message would have 3 tool_calls and
    history would have 0 tool messages → OpenAI 400 on next iteration."""
    registry = MagicMock()
    registry.get.return_value = MagicMock(is_concurrency_safe=lambda: False)
    sess = _make_session(registry)

    call_count = {"n": 0}

    async def flaky_execute(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated crash mid-turn")
        return _StubResult(output=f"ok:{kwargs['tool_name']}")

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", flaky_execute)

    pending = [
        ToolCall(id="c1", name="memory_save", input={}),
        ToolCall(id="c2", name="memory_save", input={}),
        ToolCall(id="c3", name="memory_save", input={}),
    ]

    with pytest.raises(RuntimeError, match="simulated crash"):
        await _drain(sess._run_pending_calls(pending))

    tool_msgs = [m for m in sess.history if m.get("role") == "tool"]
    ids = [m["tool_call_id"] for m in tool_msgs]
    # All three call_ids must be represented — even though c2 crashed and
    # c3 never ran, both need placeholders so the next adapter request
    # passes the function_call/output pairing invariant.
    assert ids == ["c1", "c2", "c3"], (
        f"expected all 3 call_ids represented, got {ids}. "
        f"Without the finally block, c2 + c3 would be missing."
    )
    # c1 succeeded, so its content is the real output; c2/c3 placeholders.
    by_id = {m["tool_call_id"]: m for m in tool_msgs}
    assert by_id["c1"]["content"] == "ok:memory_save"
    assert "execution failed" in by_id["c2"]["content"].lower() \
        or "interrupted" in by_id["c2"]["content"].lower()
    assert "execution failed" in by_id["c3"]["content"].lower() \
        or "interrupted" in by_id["c3"]["content"].lower()


@pytest.mark.asyncio
async def test_pending_calls_history_has_placeholders_on_cancellation(monkeypatch):
    """Cancellation path still works — call_ids that didn't run get the
    canonical `[cancelled by operator]` placeholder."""
    registry = MagicMock()
    registry.get.return_value = MagicMock(is_concurrency_safe=lambda: False)
    sess = _make_session(registry)

    async def slow_execute(**kwargs):
        if kwargs["tool_name"] == "first":
            return _StubResult(output="ok:first")
        await asyncio.sleep(10)  # never finishes — gets cancelled
        return _StubResult(output="never")

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", slow_execute)

    pending = [
        ToolCall(id="c1", name="first", input={}),
        ToolCall(id="c2", name="second", input={}),
        ToolCall(id="c3", name="third", input={}),
    ]

    async def runner():
        async for _ in sess._run_pending_calls(pending):
            pass

    task = asyncio.create_task(runner())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    tool_msgs = [m for m in sess.history if m.get("role") == "tool"]
    ids = [m["tool_call_id"] for m in tool_msgs]
    assert set(ids) == {"c1", "c2", "c3"}
    by_id = {m["tool_call_id"]: m for m in tool_msgs}
    # c2 / c3 didn't get to run — must carry the operator-cancel placeholder.
    assert "[cancelled by operator]" in by_id["c2"]["content"]
    assert "[cancelled by operator]" in by_id["c3"]["content"]


@pytest.mark.asyncio
async def test_no_double_history_append_on_success(monkeypatch):
    """Guard rail: success path must not double-append. The finally block
    should be a no-op when `history_written` is already complete."""
    registry = MagicMock()
    registry.get.return_value = MagicMock(is_concurrency_safe=lambda: False)
    sess = _make_session(registry)

    async def fake_execute(**kwargs):
        return _StubResult(output="ok")

    monkeypatch.setattr("tesseract.brain.chat.execute_tool", fake_execute)

    pending = [ToolCall(id="c1", name="t", input={}), ToolCall(id="c2", name="t", input={})]
    await _drain(sess._run_pending_calls(pending))

    tool_msgs = [m for m in sess.history if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
