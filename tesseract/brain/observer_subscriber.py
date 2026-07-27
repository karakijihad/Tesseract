"""Background activation wrapper for the stateful Observer.

Single subscriber instance per app; one attached session at a time
(single-operator deployment). `detach()` cancels in-flight tasks and
waits briefly, so arm/disarm cycles leave zero leaked asyncio tasks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from tesseract.brain.memory_suggestion import MemorySuggestion

logger = logging.getLogger(__name__)

EmitFn = Callable[[MemorySuggestion], Awaitable[None]]

_DETACH_TIMEOUT_S = 2.0


class ObserverSubscriber:
    def __init__(self, observer: Any) -> None:
        self._observer = observer
        self._active: bool = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._chat_session: Any | None = None
        self._emit: EmitFn | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    def attach(self, chat_session: Any, emit_fn: EmitFn) -> None:
        self._chat_session = chat_session
        self._emit = emit_fn
        self._active = True

    async def detach(self) -> None:
        self._active = False
        self._chat_session = None
        self._emit = None
        if not self._tasks:
            return
        tasks = list(self._tasks)
        self._tasks.clear()
        for t in tasks:
            if not t.done():
                t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=_DETACH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "observer subscriber: %d task(s) still pending after detach timeout",
                len(tasks),
            )

    def on_loop_end(self, new_turns: list[dict[str, Any]]) -> None:
        if not self._active or not new_turns:
            return
        task = asyncio.create_task(self._run(new_turns))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, new_turns: list[dict[str, Any]]) -> None:
        try:
            suggestion = await self._observer.observe_incremental(new_turns)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("observer.observe_incremental failed in subscriber")
            return
        if suggestion is None or not self._active:
            return
        # Gate UI emit on ingest dedupe: if ingest_memory_suggestion returns
        # False, the same observation_id was already queued — emitting again
        # would push a duplicate suggestion to the frontend.
        accepted = True
        if self._chat_session is not None:
            try:
                accepted = bool(self._chat_session.ingest_memory_suggestion(suggestion))
            except Exception:
                logger.exception("ingest_memory_suggestion failed")
                accepted = False
        if not accepted:
            return
        if self._emit is not None:
            try:
                await self._emit(suggestion)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("observer suggestion emit failed")
