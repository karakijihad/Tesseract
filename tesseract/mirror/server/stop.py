"""One stop, reached from every surface.

Breaking the loop is one act. Every turn this session is running ends and the
tools stop between calls. The session itself survives, and so does everything
outside it: the observer, the scheduler, autonomy, the panel writer. What
comes back is a session that looks the way it would if the turns had simply
finished.

Reached by the cockpit's Stop button, `/stop` typed in the cockpit, and
`/stop` on a channel. One implementation, because a stop that means something
different on a phone is not a stop.

Six properties held together, and a change to one of them has to be checked
against all six:

1. What the operator typed is not thrown away. A message they queued while
   the turn ran is a whole next turn addressed to the SESSION, and the
   session is still here: it waits, and it runs when the stopped turn has
   unwound (`turn_runner`'s tail drains after a cancel for that reason).
   A mid-turn injection is the other case and is cleared here, because it was
   addressed to the turn that is ending. That is the whole rule: words meant
   for this turn go with it, words meant for the next one wait for it.
2. Tools stop between calls. Every chat session's `cancel_event` is set, which
   is what a long tool loop reads; `task.cancel()` alone only lands at the
   next await.
3. It returns at once. The tasks are cancelled and NOT awaited: unwinding can
   take as long as a running thread takes, and the operator's confirmation
   must not wait on it.
4. No new turn overlaps one that is still unwinding. This is the property
   that says why the registries are NOT emptied here. Emptying them made the
   session read idle immediately, which is what the operator asked for, but a
   cancelled task is not finished until it reaches its next await, and a
   message arriving before then started a SECOND turn against the same
   ChatSession. Two turns then wrote one history. So a stopped turn keeps its
   slot until its own `finally` calls `release_turn_slot`, which is also
   identity-guarded, and the next message waits for that. In the normal case
   it is one pass of the event loop. When it is longer it is because a tool
   is inside a thread that cannot be interrupted, and waiting is the only
   correct answer there.
5. It is idempotent, and it counts TURNS rather than tasks. Pressing stop
   twice, or stopping a session with nothing running, is not an error and
   reports nothing stopped. `_what_is_running` says why the two are the same
   question, and why "Stopped 2 turns" for one message on a phone was a
   counting bug rather than a second turn.
6. Background spawns keep running. A stop ends the turns of THIS session;
   killing a delegate that has been working for ten minutes is a different
   act, and it needs its own gesture and its own name rather than riding
   along on this one. The loop that used to be here called `handle.cancel()`,
   which `SpawnHandle` does not have, so it raised on every stop and
   cancelled nothing: what is written down now is what always happened.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


def _chat_sessions(session: Any) -> list[Any]:
    """The active chat session plus every background one, no duplicates."""
    candidates = [getattr(session, "chat_session", None)]
    candidates.extend((getattr(session, "chats", None) or {}).values())
    seen: dict[int, Any] = {}
    for chat_session in candidates:
        if chat_session is not None:
            seen.setdefault(id(chat_session), chat_session)
    return list(seen.values())


def _still_to_stop(registry: Any) -> dict[Any, list[asyncio.Task[None]]]:
    """The tasks in one registry that a stop has not already reached.

    Neither finished nor already cancelled. Excluding the second is what
    makes pressing stop twice during a slow unwind report nothing the second
    time: a cancelled task is not `done()` until it reaches its next await,
    so it is still in the registry and reads as running.

    `pending_turn_tasks` holds a SET per chat rather than one task, so the
    values are flattened. Without reading it at all, a stop landing in a
    turn's setup window, or while its handler is still queued on the chat
    lock, finds nothing and says so while the turn it was meant to stop goes
    on to run.
    """
    out: dict[Any, list[asyncio.Task[None]]] = {}
    for key, value in (registry or {}).items():
        entries = value if isinstance(value, (set, list, tuple)) else (value,)
        live = [
            t for t in entries
            if t is not None and not t.done() and not t.cancelling()
        ]
        if live:
            out[key] = live
    return out


def _what_is_running(session: Any) -> tuple[list[asyncio.Task[None]], int]:
    """Every task to cancel, and how many TURNS that actually is.

    **Two questions, and they have different answers.** What to cancel is
    every task, because a turn that survives its handler is a turn that goes
    on running. What to REPORT is how many turns the operator was waiting on,
    and on a channel one turn is always two tasks: the inbound handler, which
    is registered so that a stop landing before the turn starts still finds
    something, and the turn the handler is running. They live in different
    registries under different keys (the handler under the channel's chat id,
    the turn under the session's), so nothing lines them up by key.

    Counting the tasks told the operator "Stopped 2 turns" for one message,
    every time, from the phone. A control that miscounts what it just did is
    a control you stop trusting, so the count is:

        turns = the workspace threads, which have no handler of their own,
                plus whichever is larger of the chats with a turn running and
                the chats with a handler in flight.

    The larger of the two is right in every shape there is. In the cockpit
    there are no handlers, so it is the running turns. On a channel each turn
    has exactly one handler, so the two agree. In the setup window there is a
    handler and no turn yet, and one turn is about to start.
    """
    current = _still_to_stop(getattr(session, "current_turn_tasks", None))
    synthetic = _still_to_stop(getattr(session, "synthetic_turn_tasks", None))
    pending = _still_to_stop(getattr(session, "pending_turn_tasks", None))

    found: dict[int, asyncio.Task[None]] = {}
    for registry in (current, synthetic, pending):
        for tasks in registry.values():
            for task in tasks:
                found.setdefault(id(task), task)
    # `current_turn_task` is a property over `current_turn_tasks` in the
    # cockpit, but the channel bridge assigns it directly, so read it too.
    single = getattr(session, "current_turn_task", None)
    if single is not None and not single.done() and not single.cancelling():
        found.setdefault(id(single), single)

    turns = len(synthetic) + max(len(current), len(pending))
    return list(found.values()), turns


def stop_session(session: Any) -> list[str]:
    """Break the loop for this session. Returns what was stopped.

    Synchronous on purpose: see property 3 in the module docstring. Nothing
    here awaits, so a caller can report the moment it returns.
    """
    stopped: list[str] = []
    chat_sessions = _chat_sessions(session)

    for chat_session in chat_sessions:
        chat_session.pending_injected_messages = []

    for chat_session in chat_sessions:
        context = getattr(chat_session, "tool_context", None)
        cancel_event = getattr(context, "cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()

    tasks, turns = _what_is_running(session)
    for task in tasks:
        task.cancel()
    if turns:
        label = "the turn" if turns == 1 else f"{turns} turns"
        stopped.insert(0, label)

    return stopped


def describe(stopped: list[str]) -> str:
    """The one sentence every surface says when a stop lands."""
    if not stopped:
        return "Nothing was running."
    return (
        f"Stopped {', '.join(stopped)}. What was already done stands. "
        "Tell me what to do next."
    )
