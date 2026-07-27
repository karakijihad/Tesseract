from __future__ import annotations

import asyncio
from typing import Any, Callable

from .types import SessionStatus, TurnResult


class CliSessionBackend:
    def __init__(
        self,
        *,
        handle: str,
        target: str,           # "claude" | "codex"
        adapter: Any,          # Claude/CodexStreamAdapter
        cwd: str,
        emit: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None = None,
        turn_timeout: float | None = None,
    ) -> None:
        self.handle = handle
        self.target = target
        self._adapter = adapter
        self._cwd = cwd
        self._emit = emit
        self._cancel_event = cancel_event
        self._turn_timeout = turn_timeout
        self._session_id: str | None = None
        self._turn = -1

    async def open(self, task: str) -> TurnResult:
        return await self._run(task)

    async def send(self, message: str) -> TurnResult:
        if self._turn < 0:
            return TurnResult(
                handle=self.handle, target=self.target, turn_index=0,
                result_text="session not opened", status=SessionStatus.ERROR,
                is_error=True,
            )
        return await self._run(message)

    async def close(self) -> None:
        # CLI state lives on disk (the provider's session JSONL); nothing
        # to tear down. A mid-flight turn is cancelled by the registry
        # via cancel_event before close() is called.
        return None

    async def _run(self, text: str) -> TurnResult:
        self._turn += 1
        acc = await self._adapter.run_turn(
            task=text, session_id=self._session_id, cwd=self._cwd,
            on_event=self._emit, cancel_event=self._cancel_event,
            turn_timeout=self._turn_timeout,
        )
        if acc.session_id:
            self._session_id = acc.session_id
        return TurnResult(
            handle=self.handle, target=self.target, turn_index=self._turn,
            result_text=acc.result_text, usage=acc.usage,
            status=SessionStatus.ERROR if acc.is_error else SessionStatus.DONE,
            is_error=acc.is_error,
        )
