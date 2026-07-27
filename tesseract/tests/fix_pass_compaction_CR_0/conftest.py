"""Shared fixtures for the CR-0 compaction-redesign suite.

Tests instantiate ``ChatSession`` with a minimal fake adapter so the
compaction logic can be exercised without a live model. The adapter
captures the last system prompt + last messages passed to ``stream``
so individual tests can assert on the structured-summary contract
without re-implementing the inspection plumbing each time.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


_DEFAULT_STRUCTURED_REPLY = (
    "## Operator goals\n"
    "- working on CR-0 compaction redesign\n"
    "## Decisions made\n"
    "- head anchor stays verbatim\n"
    "## Files touched\n"
    "- tesseract/brain/chat.py\n"
    "## Facts learned\n"
    "- operator prefers structured summaries\n"
    "## Open threads\n"
    "- still need to wire mirror status\n"
)


class FakeAdapter(ModelAdapter):
    """Records the messages sent and returns a configurable reply.

    The default reply is a valid 5-section structured summary, which
    lets compaction-flow tests run without each one specifying its
    own payload. Tests that need to assert on the prompt itself read
    ``self.last_messages`` / ``self.last_system``.
    """

    def __init__(self, reply: str = _DEFAULT_STRUCTURED_REPLY) -> None:
        self._reply = reply
        self.last_messages: list[dict[str, Any]] = []
        self.last_system: str = ""
        self.call_count = 0

    def set_reply(self, reply: str) -> None:
        self._reply = reply

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.call_count += 1
        self.last_messages = list(messages)
        if messages and messages[0].get("role") == "system":
            content = messages[0].get("content", "")
            self.last_system = content if isinstance(content, str) else ""
        yield StreamChunk(type=ChunkType.TEXT, text=self._reply)
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn", raw={"usage": {}})

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for m in messages:
            c = m.get("content", "")
            if isinstance(c, str):
                total += max(1, len(c) // 4)
        return total

    async def check_available(self) -> bool:
        return True


def make_user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def make_assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}
