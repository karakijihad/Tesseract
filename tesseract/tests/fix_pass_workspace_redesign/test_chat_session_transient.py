"""ChatSession.send(transient=True) — synthetic workspace turn must not
pollute conversation history.

The workspace synthetic turn fires when the operator drops a comment on
an inbox row. We want the comment + TARS's `workspace_reply` to land in
the comment thread, not the chat. The mechanism is `transient=True`:
both the appended user message and any assistant turns that follow it
are stripped from `self.history` in the `finally` block.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


class _FakeAdapter(ModelAdapter):
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        yield StreamChunk(type=ChunkType.TEXT, text="ok")
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn", raw={"usage": {}})

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return sum(len(str(m.get("content", ""))) for m in messages) // 4

    async def check_available(self) -> bool:
        return True


def _make_session() -> ChatSession:
    return ChatSession(
        adapter=_FakeAdapter(),
        system_prompt="",
        max_tool_iterations=4,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=400_000),
    )


@pytest.mark.asyncio
async def test_transient_send_does_not_persist_user_or_assistant() -> None:
    cs = _make_session()
    async for _ in cs.send("synthetic directive", transient=True):
        pass
    assert cs.history == [], "transient turn must not append to history"


@pytest.mark.asyncio
async def test_non_transient_send_persists_history() -> None:
    cs = _make_session()
    async for _ in cs.send("hello"):
        pass
    # Sanity: regular path still records user + assistant turns. Without
    # this counter-test, a buggy `transient=False` default could ship
    # silently and break the chat conversation.
    assert any(m.get("role") == "user" for m in cs.history)
    assert any(m.get("role") == "assistant" for m in cs.history)


@pytest.mark.asyncio
async def test_transient_send_skips_observer_notify() -> None:
    """The synthetic turn must NOT call the observer subscriber, and the
    observer watermark must not advance past the post-rollback history
    length. Otherwise (a) the observer fabricates a suggestion from a
    turn that's about to be erased, and (b) the next real turn's slice
    is empty because the watermark sits past EOF."""

    class _FakeSub:
        def __init__(self) -> None:
            self.is_active = True
            self.calls: list[list[dict[str, Any]]] = []

        def on_loop_end(self, turns: list[dict[str, Any]]) -> None:
            self.calls.append(turns)

    cs = _make_session()
    sub = _FakeSub()
    cs._observer_subscriber = sub  # type: ignore[attr-defined]
    cs._observer_last_index = 0  # type: ignore[attr-defined]

    async for _ in cs.send("synthetic", transient=True):
        pass

    assert sub.calls == [], "observer must not see synthetic workspace turns"
    assert cs.history == []
    assert cs._observer_last_index <= len(cs.history), (  # type: ignore[attr-defined]
        "watermark must not advance past post-rollback history length"
    )


@pytest.mark.asyncio
async def test_transient_send_followed_by_real_send_keeps_real() -> None:
    cs = _make_session()
    async for _ in cs.send("first real"):
        pass
    history_after_first = list(cs.history)
    async for _ in cs.send("synthetic", transient=True):
        pass
    assert cs.history == history_after_first, "transient must roll back cleanly"
    async for _ in cs.send("second real"):
        pass
    user_turns = [m for m in cs.history if m.get("role") == "user"]
    assert [m["content"] for m in user_turns] == ["first real", "second real"]
