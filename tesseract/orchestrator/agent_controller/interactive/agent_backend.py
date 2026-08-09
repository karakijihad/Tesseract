from __future__ import annotations

from typing import Any, Callable

from tesseract.kernel.adapters.base import ChunkType
from .types import SessionStatus, TurnResult


class AgentSessionBackend:
    """Multi-turn session over a held sub-ChatSession (own markdown agent)."""

    def __init__(
        self,
        *,
        handle: str,
        target: str,
        chat_session: Any,
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        self.handle = handle
        self.target = target
        self._session = chat_session
        self._emit = emit
        self._turn = -1

    async def open(self, task: str) -> TurnResult:
        return await self._run(task)

    async def send(self, message: str) -> TurnResult:
        return await self._run(message)

    async def close(self) -> None:
        self._session = None

    async def _run(self, text: str) -> TurnResult:
        self._turn += 1
        if self._session is None:
            return TurnResult(
                handle=self.handle,
                target=self.target,
                turn_index=self._turn,
                result_text="session closed",
                status=SessionStatus.ERROR,
                is_error=True,
            )
        parts: list[str] = []
        is_error = False
        async for chunk in self._session.send(text):
            if chunk.type == ChunkType.TEXT:
                parts.append(chunk.text)
                self._emit({"type": "assistant", "text": chunk.text})
            elif chunk.type == ChunkType.ERROR:
                is_error = True
                self._emit({"type": "error", "text": chunk.error})
        return TurnResult(
            handle=self.handle,
            target=self.target,
            turn_index=self._turn,
            result_text="".join(parts).strip(),
            status=SessionStatus.ERROR if is_error else SessionStatus.DONE,
            is_error=is_error,
        )
