"""ChatSession timestamps are persisted but not sent to adapters."""

from __future__ import annotations

from typing import AsyncGenerator

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter, StreamChunk


class _CaptureAdapter(ModelAdapter):
    def __init__(self) -> None:
        self.seen_messages: list[dict] = []

    async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
        return "unused"

    async def stream(
        self,
        messages,
        tools=None,
        options=None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.seen_messages = list(messages)
        yield StreamChunk(type=ChunkType.TEXT, text="hello back")
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")

    def count_tokens(self, messages) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


async def test_chat_session_keeps_timestamps_in_history_but_not_adapter_payload() -> None:
    adapter = _CaptureAdapter()
    session = ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
    )

    async for _chunk in session.send("hello there"):
        pass

    assert session.history[0]["timestamp"]
    assert session.history[1]["timestamp"]
    assert all("timestamp" not in msg for msg in adapter.seen_messages)
