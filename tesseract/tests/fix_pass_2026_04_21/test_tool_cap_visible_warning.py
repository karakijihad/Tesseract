"""When the tool-iteration cap is hit, `chat.py::send` must NOT break the
turn. Operator policy (2026-05-19): reset the counter, yield a soft notice
(orb stays calm), and keep streaming. Daily cost caps and the
consecutive-adapter-error breaker remain the safety net against runaway spend.

Pins three contracts:
1. After cap, a soft ERROR chunk fires with reason='tool_cap_reset' and a
   monotonically increasing `resets` counter.
2. The loop continues past the cap (we observe ≥2 resets when the adapter
   keeps emitting tool calls).
3. The wrap-up nudge still appends on each iteration immediately before a
   reset, giving the model a graceful-exit hint.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter, StreamChunk
from tesseract.kernel.state import ToolCall
from tesseract.kernel.tools.base import ToolContext


class _LoopingAdapter(ModelAdapter):
    """Always emits one tool call — forces the tool-iteration cap to fire repeatedly."""

    def __init__(self) -> None:
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.seen_messages.append(list(messages))
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


async def test_tool_cap_emits_soft_reset_and_keeps_looping() -> None:
    adapter = _LoopingAdapter()
    cap = 3  # tiny cap keeps the test fast
    cs = ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=cap,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake"),
        registry=None,
        tool_context=ToolContext(),
    )

    chunks: list[StreamChunk] = []
    async for ch in cs.send("force the cap"):
        chunks.append(ch)
        resets = [
            c for c in chunks
            if c.type is ChunkType.ERROR
            and c.raw and c.raw.get("reason") == "tool_cap_reset"
        ]
        # Observe two resets to prove the loop survived the cap; then break
        # out (the async generator closes cleanly via GeneratorExit).
        if len(resets) >= 2:
            break

    resets = [
        c for c in chunks
        if c.type is ChunkType.ERROR
        and c.raw and c.raw.get("reason") == "tool_cap_reset"
    ]
    assert len(resets) >= 2, (
        f"BUG: tool-cap broke the turn instead of resetting and continuing. "
        f"resets={len(resets)} chunks={[c.type for c in chunks]!r}"
    )

    first = resets[0]
    assert first.raw and first.raw.get("severity") == "soft", (
        f"BUG: cap-reset must be severity='soft' so the orb stays calm. raw={first.raw!r}"
    )
    assert first.raw.get("resets") == 1, (
        f"BUG: first reset must report resets=1. raw={first.raw!r}"
    )
    assert "reset" in first.error.lower() and str(cap) in first.error, (
        f"BUG: cap-reset error text drifted — {first.error!r}"
    )

    second = resets[1]
    assert second.raw and second.raw.get("resets") == 2, (
        f"BUG: second reset must report resets=2 (monotonic). raw={second.raw!r}"
    )

    # Wrap-up nudge still fires on every cap-1 iteration so the model gets a
    # graceful-exit hint before the reset. Check the first cycle: the
    # adapter saw `cap` calls in iterations 0..cap-1; iteration cap-1 must
    # carry the nudge, earlier iterations must not.
    assert len(adapter.seen_messages) >= cap, (
        f"adapter should have been called at least {cap} times; got {len(adapter.seen_messages)}"
    )
    last_pre_reset_msgs = adapter.seen_messages[cap - 1]
    system_contents = [
        m.get("content", "") for m in last_pre_reset_msgs if m.get("role") == "system"
    ]
    assert any("final iteration" in s.lower() for s in system_contents), (
        f"BUG: wrap-up nudge missing on cap-1 iteration — model never told to wrap up. "
        f"system messages: {system_contents!r}"
    )
    for i, msgs in enumerate(adapter.seen_messages[: cap - 1]):
        sys_contents = [m.get("content", "") for m in msgs if m.get("role") == "system"]
        assert not any("final iteration" in s.lower() for s in sys_contents), (
            f"BUG: wrap-up nudge leaked into iteration {i} (not cap-1) — "
            f"model would stop searching early."
        )
