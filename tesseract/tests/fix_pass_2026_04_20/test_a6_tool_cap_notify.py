"""A6 — tool-cap reset path still notifies _notify_observer_turn_end.

Claude coder H-3 (original): in chat.py send(), the normal-exit path called
_notify_observer_turn_end(); the MAX_TOOL_ITERATIONS path used to fall
through to finally without it. Now (2026-05-19) the cap doesn't terminate
the turn at all — it soft-resets and continues — but the observer-notify
contract still has to fire when the generator is closed.

Simulates the loop by driving ChatSession via a fake adapter that always
emits a tool call. The test breaks out of the async-generator after
observing a reset, which triggers the `finally` block. Without the fix,
the observer would be blind to the whole turn.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter, StreamChunk
from tesseract.kernel.state import ToolCall
from tesseract.kernel.tools.base import ToolContext


class _LoopingToolAdapter(ModelAdapter):
    """Always emits one tool call — forces MAX_TOOL_ITERATIONS."""

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        yield StreamChunk(type=ChunkType.TEXT, text="thinking...")
        yield StreamChunk(
            type=ChunkType.TOOL_CALL_END,
            tool_call_id="call_1",
            tool_call=ToolCall(id="call_1", name="noop", input={}),
        )
        yield StreamChunk(type=ChunkType.STOP, stop_reason="tool_use", raw={"usage": {}})

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return sum(len(str(m.get("content", ""))) // 4 for m in messages)

    async def check_available(self) -> bool:
        return True


class _FakeSubscriber:
    def __init__(self) -> None:
        self.is_active = True
        self.received: list[list[dict[str, Any]]] = []

    def on_loop_end(self, new_turns):
        self.received.append(list(new_turns))


async def test_a6_tool_cap_notify() -> None:
    sub = _FakeSubscriber()
    # registry=None drives _run_pending_calls into the "no tool registry"
    # branch, which synthesises a tool-result and keeps the loop alive —
    # so the adapter's unconditional tool_call emission cycles until the
    # MAX_TOOL_ITERATIONS cap is hit.
    cs = ChatSession(
        adapter=_LoopingToolAdapter(),
        system_prompt="",
        max_tool_iterations=3,  # tiny cap keeps the test fast
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake"),
        registry=None,
        tool_context=ToolContext(),
    )
    cs.attach_observer_subscriber(sub)

    # Drive the turn. The looping adapter never stops emitting tool calls,
    # so the new while-loop would run forever — break out as soon as we
    # observe a tool-cap reset envelope (proof the cap fired at least
    # once). `async for ... break` does NOT auto-call the generator's
    # `aclose()` per PEP 525 (it's left to GC), so the `finally` block
    # that fires `_notify_observer_turn_end` would never run inside the
    # test's event loop. Driving the generator manually + explicit
    # `aclose()` is the only way to force the finally to execute before
    # the assertion runs.
    from tesseract.kernel.adapters.base import ChunkType  # local import — narrow scope
    gen = cs.send("force a tool-cap loop")
    try:
        async for chunk in gen:
            if (
                chunk.type is ChunkType.ERROR
                and chunk.raw
                and chunk.raw.get("reason") == "tool_cap_reset"
            ):
                break
    finally:
        await gen.aclose()

    # After FIX: the finally block fires _notify_observer_turn_end, so
    # the subscriber receives at least one call even after hitting the
    # iteration cap.
    assert len(sub.received) >= 1, (
        f"BUG (A6): tool-cap exit did not notify observer "
        f"(subscriber.received={len(sub.received)} calls)"
    )
    # The notified delta should include the fake user turn + at least one
    # assistant message recorded before the cap.
    flat = [m for call in sub.received for m in call]
    has_user = any(m.get("role") == "user" and "force" in str(m.get("content", "")) for m in flat)
    has_assistant = any(m.get("role") == "assistant" for m in flat)
    assert has_user and has_assistant, (
        f"BUG (A6): notify missing user+assistant turns; got {flat}"
    )
