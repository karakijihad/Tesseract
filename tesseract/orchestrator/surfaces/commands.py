"""Commands to a card, and the answer coming back.

The canvas already had both directions. ``publish_surface_event`` carries
verbs out to every connected operator, and the render-report route carries one
fact back. What neither did is pair them: a tool could ask a card to exist and
could read whether it drew, but could not ask a card to DO something and learn
what happened.

This is the pairing. A command goes out on the existing bus carrying an id; the
renderer that owns the card acts and posts the outcome back against that id;
the waiting tool wakes with the card's own state rather than with a hope.

**Why the tool waits at all.** IS-12's rule: a verb should return enough state
that the model does not need a screenshot round trip to know it worked. A
fire-and-forget "sent" makes the model guess, and a guessing model takes
another turn to check.

Bounded on purpose. A command nobody answers expires rather than pinning a
future forever, and the tool says plainly that it went out unanswered, which is
a different sentence from "it failed".
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

#: How long a tool waits for a card to answer. Generous next to a DOM call and
#: a fetch, short next to a person waiting for the assistant to say something.
DEFAULT_TIMEOUT_S = 3.0

#: A card that never answers must not pin memory. Reached only if something is
#: issuing commands nothing renders, which is itself worth the log line.
MAX_PENDING = 64


class CommandBroker:
    """Issues card commands and matches answers to them by id."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def issue(self) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        """A fresh id and the future its answer will land in."""
        if len(self._pending) >= MAX_PENDING:
            # Oldest first: a backlog this size means answers are not coming,
            # and the newest command is the one someone is waiting on.
            stale, fut = next(iter(self._pending.items()))
            self._pending.pop(stale, None)
            if not fut.done():
                fut.cancel()
            log.warning("surface command: dropped stale pending id %s", stale)
        command_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[command_id] = fut
        return command_id, fut

    def resolve(self, command_id: str, result: dict[str, Any]) -> bool:
        """Hand a card's answer to whoever is waiting. False when nobody is:
        a late answer after a timeout, or an id nothing issued."""
        fut = self._pending.pop(command_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(result)
        return True

    async def wait(
        self, command_id: str, fut: asyncio.Future[dict[str, Any]], timeout: float
    ) -> dict[str, Any] | None:
        """The answer, or None when the card did not give one in time."""
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        finally:
            self._pending.pop(command_id, None)

    def clear(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()


_broker: CommandBroker | None = None


def get_command_broker() -> CommandBroker:
    global _broker
    if _broker is None:
        _broker = CommandBroker()
    return _broker


def reset_command_broker() -> None:
    """Test-only."""
    global _broker
    if _broker is not None:
        _broker.clear()
    _broker = None


__all__ = [
    "CommandBroker",
    "DEFAULT_TIMEOUT_S",
    "get_command_broker",
    "reset_command_broker",
]
