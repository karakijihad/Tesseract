"""The conversation the machine died inside takes its own work back up.

The boot pass decides what may be repeated and hands the answer to
`recovery/resume.py`. This is the door that gets a turn to read it. Without it
the whole acting half waits on somebody typing into the right chat, which is
the opposite of the operator's own instruction: *"if the system crashes, it
should pickup it and continue"*.

**It cannot fire at boot, and that is the shape rather than a gap.** Recovery
runs inside the boot graph, where no cockpit session exists yet: a chat is a
live object built when a window connects. So the boot writes down which
conversations are owed a resumed turn (`resume.note_owed`) and this fires when
one of them comes back. The same two stages `spawn_wake` and `card_wake` use,
for the same reason: a fact arrives while nobody is looking, and it has to
reach one specific conversation.

**Three gates, and each closes a different hole.**

  The boot's own list. Only a run `_scan_turns` closed as interrupted puts a
  chat on it, so this is about the crash that just happened and never about a
  call from three weeks ago that nothing ever closed.

  `resume.claim`. One telling per conversation, and it hands back the RUNS
  the boot named so the brief is about this crash rather than about everything
  the conversation ever left unrecorded. A chat restores into every window the
  operator opens, and the same brief twice is the runtime saying the same
  thing again.

  The breaker. A chat that fails its resumed turn repeatedly stops being woken,
  which is the rule every other wake in this package already follows.

**The turn is drawn as the runtime's.** `runtime_origin="recovery"` is stamped
on the message so the transcript draws a rule rather than a bubble wearing the
operator's name (`brain/chat.py::RUNTIME_ORIGINS`, `RuntimeNote.tsx`). Nobody
typed this; the record should not say they did.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from tesseract.context.circuit_breaker import CircuitBreaker
from tesseract.mirror.server import wake_turn
from tesseract.orchestrator.recovery import resume
from tesseract.paths import log_dir

logger = logging.getLogger(__name__)

#: One global breaker, not one per chat, for the reason `card_wake`'s is: a
#: per-chat breaker writes a JSONL per conversation.
_WAKE_BREAKER_NAME = "resume-wake"
_wake_breaker: CircuitBreaker | None = None


def _get_wake_breaker() -> CircuitBreaker:
    """The breaker, built on first use. `log_dir` resolves `TESSERACT_HOME` at
    call time, so a test that redirects home lands its JSONL under its own tmp
    dir rather than in the operator's logs."""
    global _wake_breaker
    if _wake_breaker is None:
        _wake_breaker = CircuitBreaker(
            name=_WAKE_BREAKER_NAME, log_dir=log_dir("circuit-breakers")
        )
    return _wake_breaker


def reset_wake_breaker() -> None:
    """Test-only."""
    global _wake_breaker
    _wake_breaker = None


def offer(app: Any, session: Any, turn_driver: Callable[..., Any] | None = None) -> int:
    """Wake every chat in this session the last process died inside. Returns how many.

    **Nothing is read off disk here.** This runs inside `create_server_session`,
    which is a plain function called from the websocket handler, so everything
    it does happens on the loop that health, heartbeats and every other
    conversation share. The set intersection and the idle check are free; the
    record is read inside the turn's own task, on a thread.

    Never raises. The caller is part-way through handing a connected window
    back to the operator, and a conversation that cannot be resumed must not
    cost them the window. Everything this does is recoverable by hand: the
    calls stay open on the record and the operator can ask about them in the
    chat.
    """
    woken = 0
    try:
        for chat_id in sorted(resume.owed() & set(getattr(session, "chats", {}))):
            if not _chat_idle(session, chat_id):
                continue
            if not _get_wake_breaker().allow(subject=f"resuming chat {chat_id}"):
                continue
            runs = resume.claim(chat_id)
            if not runs:
                continue
            _schedule(app, session, chat_id, runs, turn_driver)
            woken += 1
    except Exception:  # noqa: BLE001 - a window is never lost to this
        logger.exception("resume wake failed for session %s", getattr(session, "session_id", "?"))
    return woken


def _chat_idle(session: Any, chat_id: str) -> bool:
    task = getattr(session, "current_turn_tasks", {}).get(chat_id)
    return task is None or task.done()


def _schedule(
    app: Any,
    session: Any,
    chat_id: str,
    runs: frozenset[str],
    turn_driver: Callable[..., Any] | None = None,
) -> None:
    """Start the resumed turn and register it as the chat's in-flight turn."""
    from tesseract.mirror.server.ws import _spawn_tracked

    task = _spawn_tracked(
        app,
        _resumed_turn(app, session, chat_id, runs, turn_driver),
        f"resume_wake:{getattr(session, 'session_id', '?')}:{chat_id}",
    )
    session.current_turn_tasks[chat_id] = task


async def _resumed_turn(
    app: Any,
    session: Any,
    chat_id: str,
    runs: frozenset[str],
    turn_driver: Callable[..., Any] | None = None,
) -> None:
    """Read what this crash left in this conversation, and take a turn on it.

    The read is here rather than in `offer` so it lands on a thread, and it is
    narrowed to the runs the boot named: a conversation read whole carries
    every call it ever left open, and the oldest of those is the one this turn
    is least able to find in its own transcript.

    An empty brief means something closed the calls between the boot pass and
    this window. The claim has already been taken, which is right: there is
    nothing to say and no later window should ask again.

    No re-check afterwards, which is where this differs from the other two
    wakes. Their subject keeps arriving: another spawn finishes, another card
    is pressed. This one's subject is a crash that already happened, and the
    turn either dealt with it or reported that it could not.
    """
    if getattr(session, "chats", {}).get(chat_id) is None:
        return
    found = await asyncio.to_thread(resume.read, chat_id, runs=runs)
    body = resume.brief(found)
    if not body:
        logger.info(
            "resume wake: nothing left open in chat %s, so no turn was taken", chat_id
        )
        return
    await wake_turn.drive(
        app,
        session,
        chat_id,
        breaker=_get_wake_breaker(),
        body=body,
        error_label="the resumed turn ended in a swallowed stream_error",
        runtime_origin="recovery",
        turn_driver=turn_driver,
    )


__all__ = ["offer", "reset_wake_breaker"]
