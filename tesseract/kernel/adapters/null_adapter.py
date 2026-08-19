"""Placeholder ``ModelAdapter`` used when no chat_brain provider resolved
(no API key set for any configured provider, or every candidate disabled in
providers.yaml).

Nothing in TESSERACT requires a cloud/API provider to be present — the app
boots and every capability that doesn't need an LLM (voice, memory, vault,
settings, alarms, schedule, tools) works with zero keys. Chat itself
genuinely cannot run without a model, so this adapter lets the rest of the
chat plumbing (ChatSession, WS connect, tool registry) construct normally,
and fails loudly with a plain-language reason the first time a turn actually
tries to generate a response — surfaced via the existing stream_error
envelope path (an uncaught adapter exception propagates out of
``ChatSession.send()`` and is turned into a ``stream_error`` envelope by
``tesseract/mirror/server/turn_runner.py::_run_turn``). No new error-reporting
mechanism needed.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter, StreamChunk


class NullChatAdapter(ModelAdapter):
    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        raise RuntimeError(self._reason)
        yield  # pragma: no cover — unreachable; keeps this an async generator

    @staticmethod
    def count_tokens(messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return False
