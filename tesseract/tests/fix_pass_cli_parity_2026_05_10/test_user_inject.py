"""Phase 2 (CLI parity) — operator messages typed mid-turn are queued on
ChatSession.pending_injected_messages and folded into history at the next
tool boundary as `[mid-turn] ...` user messages, with a USER_INJECT
StreamChunk yielded so the WS layer can fire `stream_user_inject`.

Tests cover the ChatSession plumbing in isolation. Full WS-level
end-to-end (envelope emission, frontend badge clearing) is exercised
via the Mirror-side integration tests and Playwright spec.
"""

from __future__ import annotations

from typing import AsyncGenerator

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


class _ScriptedAdapter(ModelAdapter):
    """Adapter that replays a fixed sequence of chunk lists, one per stream() call."""

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls: list[list[dict]] = []

    async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
        return ""

    async def stream(self, messages, tools=None, options=None) -> AsyncGenerator[StreamChunk, None]:
        self.calls.append(list(messages))
        if not self.scripts:
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")
            return
        for chunk in self.scripts.pop(0):
            yield chunk

    def count_tokens(self, messages) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _new_session(adapter: ModelAdapter) -> ChatSession:
    return ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=5,
        max_consecutive_adapter_errors=3,
    )


def test_enqueue_user_inject_queues_text_with_timestamp() -> None:
    sess = _new_session(_ScriptedAdapter([]))
    sess.enqueue_user_inject("hello")
    sess.enqueue_user_inject("  follow up  ")
    sess.enqueue_user_inject("")  # ignored
    sess.enqueue_user_inject("   ")  # ignored

    assert len(sess.pending_injected_messages) == 2
    assert sess.pending_injected_messages[0]["text"] == "hello"
    assert sess.pending_injected_messages[1]["text"] == "follow up"
    assert "queued_at" in sess.pending_injected_messages[0]


def test_drain_user_injections_folds_into_history_with_mid_turn_prefix() -> None:
    sess = _new_session(_ScriptedAdapter([]))
    sess.enqueue_user_inject("first queued")
    sess.enqueue_user_inject("second queued")

    drained = sess._drain_user_injections()
    assert [d["text"] for d in drained] == ["first queued", "second queued"]
    assert sess.pending_injected_messages == []

    assert len(sess.history) == 2
    assert sess.history[0]["role"] == "user"
    assert sess.history[0]["content"] == "[mid-turn] first queued"
    assert sess.history[0]["_mid_turn"] is True
    assert sess.history[1]["content"] == "[mid-turn] second queued"


def test_drain_returns_empty_list_when_queue_empty() -> None:
    sess = _new_session(_ScriptedAdapter([]))
    assert sess._drain_user_injections() == []
    assert sess.history == []


def test_reset_clears_pending_injected_messages() -> None:
    sess = _new_session(_ScriptedAdapter([]))
    sess.enqueue_user_inject("queued")
    sess.history.append({"role": "user", "content": "earlier"})

    sess.reset()

    assert sess.pending_injected_messages == []
    assert sess.history == []


class _NoopSchema:
    """Pydantic-free placeholder; the test uses an empty input dict."""


@pytest.mark.asyncio
async def test_send_yields_user_inject_chunk_after_tool_boundary() -> None:
    """Operator queues a message between iterations 1 and 2 (between
    the tool result and the next adapter call). Verify the loop:
      1. Yields the tool-result chunk.
      2. Drains the queue and yields a USER_INJECT chunk.
      3. Re-enters adapter.stream with the injected user message visible.
    """
    from typing import ClassVar

    from pydantic import BaseModel

    from tesseract.brain.tools import ToolRegistry
    from tesseract.kernel.state import ToolCall
    from tesseract.kernel.tools.base import (
        PermissionResult,
        Tool,
        ToolContext,
        ToolResult,
    )

    class _NoopInput(BaseModel):
        pass

    class _NoopTool(Tool):
        default_posture: ClassVar[str] = "auto"
        risk_class: ClassVar[str] = "autonomous"

        @property
        def name(self) -> str:
            return "noop"

        @property
        def description(self) -> str:
            return "noop test tool"

        @property
        def input_schema(self) -> type[BaseModel]:
            return _NoopInput

        def is_concurrency_safe(self) -> bool:
            return True

        def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
            return PermissionResult.ALLOW

        async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
            return ToolResult(output="ok", is_error=False)

    registry = ToolRegistry()
    registry.register(_NoopTool())

    iter1 = [
        StreamChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call_id="call-1",
            tool_call=ToolCall(id="call-1", name="noop", input={}),
        ),
        StreamChunk(
            type=ChunkType.TOOL_CALL_END,
            tool_call_id="call-1",
            tool_call=ToolCall(id="call-1", name="noop", input={}),
        ),
        StreamChunk(type=ChunkType.STOP, stop_reason="tool_use"),
    ]
    iter2 = [
        StreamChunk(type=ChunkType.TEXT, text="ack"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ]

    adapter = _ScriptedAdapter([iter1, iter2])
    sess = _new_session(adapter)
    sess.registry = registry
    sess.tool_context.session_id = "test-cli-parity-1"

    chunks: list[StreamChunk] = []

    async def _consume() -> None:
        # Operator queues a message AFTER the tool result fires but
        # BEFORE iter2 starts. Inject it inline by hooking into the
        # stream — simulating the WS-layer enqueue.
        async for c in sess.send("first message"):
            chunks.append(c)
            if c.type is ChunkType.TOOL_RESULT:
                sess.enqueue_user_inject("queued mid-turn")

    await _consume()

    inject_chunks = [c for c in chunks if c.type is ChunkType.USER_INJECT]
    assert len(inject_chunks) == 1
    payload = inject_chunks[0].raw or {}
    assert payload.get("count") == 1
    assert payload["injected"][0]["text"] == "queued mid-turn"

    # The mid-turn user message must be in history before the second
    # adapter call's messages were captured.
    second_call_messages = adapter.calls[1] if len(adapter.calls) >= 2 else []
    mid_turn_in_call = any(
        isinstance(m.get("content"), str) and "[mid-turn] queued mid-turn" in m["content"]
        for m in second_call_messages
    )
    assert mid_turn_in_call, "mid-turn message must reach the model on the next iteration"
