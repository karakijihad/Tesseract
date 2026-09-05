"""Which turn is running, for anything below the turn that writes a record.

The cost ledger sits several layers under `ChatSession.send` and is handed
nothing that names a turn, so every row it wrote said which ROLE paid and
never which piece of work. This is the one seam that answers it: `send` binds
its recorder here when the turn's own record opens, and the ledger reads the
identity back at write time.

Invariants, held together:

1. Bound at the one seam both funnels pass through, so the cockpit and every
   channel get it without either learning about the other.
2. Task-local. `asyncio.create_task` snapshots the context, so a scheduled
   job or a boot probe running beside a turn never sees the turn's identity
   and its rows say so, rather than being attributed to whatever ran last.
3. Read live. The task a turn works is bound part-way through it, so the
   identity is read off the recorder at each write rather than copied once.
4. Leaving clears, never restores. Turns on a session are serial and never
   nest, so there is no earlier binding worth handing back; a `reset` to it
   would re-bind a dead recorder if one ever survived a turn, and it raises
   when `send`, an async generator, is finalised in another context. Setting
   the variable to nothing does neither, and doing it twice is nothing.
5. Imports nothing from the package, so the ledger can read it without
   pulling the orchestrator in under it.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

_current_turn: ContextVar[Any] = ContextVar("tesseract.current_turn", default=None)


@dataclass(frozen=True)
class TurnIdentity:
    """What a record written during a turn can say about the turn. Empty
    strings outside one, and that emptiness is the claim."""

    turn_id: str = ""
    task_id: str = ""


def enter_turn(recorder: Any) -> None:
    """Bind the running turn's recorder."""
    _current_turn.set(recorder)


def leave_turn() -> None:
    """Unbind whatever is bound. Idempotent, and never raises: invariant 4."""
    _current_turn.set(None)


def turn_identity() -> TurnIdentity:
    """The turn this code is running under, or nothing."""
    recorder = _current_turn.get()
    if recorder is None:
        return TurnIdentity()
    try:
        return TurnIdentity(
            turn_id=str(getattr(recorder, "turn_id", "") or ""),
            task_id=str(getattr(recorder.manifest, "task_id", "") or ""),
        )
    except AttributeError:
        return TurnIdentity()


__all__ = ["TurnIdentity", "enter_turn", "leave_turn", "turn_identity"]
