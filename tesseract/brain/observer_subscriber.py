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
from typing import Any

logger = logging.getLogger(__name__)

_CANCEL_TIMEOUT_S = 2.0


def _generation_of(chat_session: Any) -> int | None:
    """Which conversation the session is holding, or `None` if it cannot say.

    A test double or an older session without the counter is not a session
    that has been wiped, so an unknown generation never discards an
    observation. Failing closed here would silently stop the observer on
    anything that did not implement it.
    """
    return getattr(chat_session, "conversation_generation", None)


def _room_of(chat_session: Any) -> Any:
    """How full the conversation is, handed to the observer by its caller.

    Same shape as the transcript: the conversation owns it and the observer is
    given it, rather than the observer reaching into a session for state that
    is not its own. Same publisher the agent's own reading comes from, so the
    two cannot disagree about the number.

    Best effort. An observer that does not know the room still has a
    conversation to read, and a missing figure must never cost an observation.
    """
    try:
        from tesseract.brain import context_signal

        return context_signal.read(getattr(chat_session, "_failures_scope_id", ""))
    except Exception:
        logger.exception("could not read the room for the observer")
        return None


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
        # Which conversation this observation is about, captured before the
        # model call rather than assumed after it.
        task = asyncio.create_task(
            self._run(new_turns, chat_session, _generation_of(chat_session))
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(
        self,
        new_turns: list[dict[str, Any]],
        chat_session: Any,
        generation: int | None = None,
    ) -> None:
        try:
            reading = await self._observer.observe_incremental(
                new_turns,
                chat_session.observer_transcript,
                room=_room_of(chat_session),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("observer.observe_incremental failed in subscriber")
            return
        if not self._active:
            return
        # A boundary clears the conversation in place, on the same object, and
        # this call outlived the turn that started it. What came back describes
        # a conversation that is gone: its suggestion names turns nobody can
        # read any more, and its nudge argues about whether work that has
        # already been handed over should stop. Neither belongs to whoever is
        # speaking now. Nothing awaits between here and the ingests below, so
        # the check cannot go stale on its way to being used.
        if generation is not None and _generation_of(chat_session) != generation:
            logger.info(
                "observation dropped: the conversation was cleared while it ran"
            )
            return
        # The two halves are independent: a nudge is delivered even when the
        # same call had nothing worth remembering, and a failure to record one
        # never costs the other. Neither goes to `observer_emit`, which carries
        # the suggestion chip alone; where a nudge is read is the journal.
        if reading.nudge is not None:
            try:
                chat_session.ingest_boundary_nudge(reading.nudge)
            except Exception:
                logger.exception("ingest_boundary_nudge failed")
        suggestion = reading.suggestion
        if suggestion is None:
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
