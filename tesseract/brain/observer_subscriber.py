"""Background activation wrapper for the Observer.

One subscriber per app, attached to every `ChatSession` the runtime
builds — cockpit or channel. It holds no session registry: the session
that fired hands itself to `on_loop_end`, carries its own transcript and
its own emit chip, so there is nothing here to key, evict or leak.

`arm()` / `disarm()` are the whole of the operator's switch and are
synchronous, so a caller with no event loop can still stop it firing.
`cancel_in_flight()` joins what is already running, so arm/disarm cycles
leave zero leaked asyncio tasks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from tesseract.brain.memory_suggestion import MemorySuggestion

logger = logging.getLogger(__name__)

EmitFn = Callable[[MemorySuggestion], Awaitable[None]]

_CANCEL_TIMEOUT_S = 2.0


class ObserverSubscriber:
    def __init__(self, observer: Any) -> None:
        self._observer = observer
        self._active: bool = False
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def is_active(self) -> bool:
        return self._active

    def set_observer(self, observer: Any) -> None:
        """Point at a rebuilt observer without replacing the subscriber.

        Every live `ChatSession` holds a back-reference taken when it was
        built, so swapping the subscriber itself would orphan every
        conversation the runtime is holding — including the ones on a
        channel, which no reconnect would ever rebuild.
        """
        self._observer = observer

    def arm(self) -> None:
        self._active = True

    def disarm(self) -> None:
        self._active = False

    async def cancel_in_flight(self) -> None:
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
                timeout=_CANCEL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "observer subscriber: %d task(s) still pending after cancel timeout",
                len(tasks),
            )

    def on_loop_end(self, new_turns: list[dict[str, Any]], chat_session: Any) -> None:
        if not self._active or not new_turns:
            return
        task = asyncio.create_task(self._run(new_turns, chat_session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, new_turns: list[dict[str, Any]], chat_session: Any) -> None:
        try:
            suggestion = await self._observer.observe_incremental(
                new_turns, chat_session.observer_transcript
            )
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
        try:
            accepted = bool(chat_session.ingest_memory_suggestion(suggestion))
        except Exception:
            logger.exception("ingest_memory_suggestion failed")
            return
        if not accepted:
            return
        emit = chat_session.observer_emit
        if emit is None:
            return
        try:
            await emit(suggestion)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("observer suggestion emit failed")
