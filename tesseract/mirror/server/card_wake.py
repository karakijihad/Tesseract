"""A press inside a card reaches the conversation that drew it.

The canvas bridge already carried presses back: the store records them and
`surface_control action:"read"` returns them. What it could not do is tell the
assistant that one had happened, so a game it had built could be played only by
the operator narrating their own moves back to it in chat. That is the
measured complaint this closes.

**The shape is `spawn_wake`'s, deliberately.** A background spawn finishing and
an operator pressing a button are the same problem: a fact arrives while nobody
is looking, and it has to reach one specific conversation. That module already
answers it in two stages, and copying the shape rather than inventing a second
one is the rule this runtime is built on.

  Stage 1, the floor. The press is queued onto the owning chat and rides into
  the next turn, whenever that is. Nothing is lost if the wake never fires.

  Stage 2, the wake. If that chat is idle, start a turn so the assistant reacts
  now instead of waiting to be spoken to. A turn already in flight needs no
  wake: the floor lands at its next iteration.

**Only a press wakes.** An `edited` fires on every change a field reports, and
a game with a text box would wake a turn per keystroke. A press is the
deliberate act. Edits are still recorded and still readable, they just do not
interrupt.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from tesseract.context.circuit_breaker import CircuitBreaker
from tesseract.mirror.server import wake_turn
from tesseract.orchestrator.surfaces.store import get_surface_store
from tesseract.paths import log_dir

logger = logging.getLogger(__name__)

#: The wake turn's body. The press itself rides in via the floor, so this only
#: has to say what happened and hand the decision back, the way the spawn
#: nudge does. Naming the control here as well would deliver it twice.
_WAKE_NUDGE = (
    "(The operator used a control on a card you drew. What they pressed is "
    "noted above. Take your turn.)"
)

#: One global breaker, not one per chat: a per-chat breaker would write a JSONL
#: per conversation. Threshold comes from the shared default.
_WAKE_BREAKER_NAME = "card-wake"
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


def install(app: Any) -> None:
    """Point the surface store's press notifier at this module.

    Once per process, at boot. The store is orchestrator-level and knows
    nothing about sessions or turns; this is the one place the two meet, which
    is why the store holds a callback rather than an import.
    """
    get_surface_store().press_notifier = (
        lambda surface_id, chat_id, entry: on_card_press(app, surface_id, chat_id, entry)
    )


def find_chat(app: Any, chat_id: str) -> tuple[Any, Any] | None:
    """The session holding this chat, and the chat itself.

    Scanned rather than indexed. A second index would be a second thing to keep
    in step with `session.chats`, and the number of open sessions is the number
    of Mirror windows the operator has open.
    """
    sessions = app.get("server_sessions") or {} if hasattr(app, "get") else {}
    for session in list(sessions.values()):
        chat = getattr(session, "chats", {}).get(chat_id)
        if chat is not None:
            return session, chat
    return None


def _pending(session: Any) -> set[str]:
    slot: set[str] | None = getattr(session, "card_wake_pending", None)
    if slot is None:
        slot = set()
        session.card_wake_pending = slot
    return slot


def on_card_press(
    app: Any,
    surface_id: str,
    chat_id: str,
    entry: dict[str, Any],
    turn_driver: Callable[..., Any] | None = None,
) -> None:
    """Floor the press onto its chat, and wake that chat if it is idle.

    Never raises: the caller is the store, mid-way through recording an event
    for a client that has already done its part. A chat that has since closed,
    a window that has gone away, a tripped breaker: each of these costs the
    proactive half and leaves the press readable, which is where it was before
    this module existed.
    """
    try:
        found = find_chat(app, chat_id)
        if found is None:
            return
        session, chat = found
        chat.ingest_card_press(
            surface_id, str(entry.get("target") or ""), entry.get("value")
        )
        if not _chat_idle(session, chat_id):
            return
        pending = _pending(session)
        if chat_id in pending:
            return
        if not _get_wake_breaker().allow(subject=f"card press in chat {chat_id}"):
            return
        pending.add(chat_id)
        schedule_wake(app, session, chat_id, turn_driver)
    except Exception:  # noqa: BLE001 — the press is recorded whatever happens here
        logger.exception("card wake failed for chat %s", chat_id)


def _chat_idle(session: Any, chat_id: str) -> bool:
    task = getattr(session, "current_turn_tasks", {}).get(chat_id)
    return task is None or task.done()


def schedule_wake(
    app: Any, session: Any, chat_id: str, turn_driver: Callable[..., Any] | None = None
) -> None:
    """Start the wake turn and register it as the chat's in-flight turn."""
    from tesseract.mirror.server.ws import _spawn_tracked

    task = _spawn_tracked(
        app,
        _wake_turn(app, session, chat_id, turn_driver),
        f"card_wake:{getattr(session, 'session_id', '?')}:{chat_id}",
    )
    session.current_turn_tasks[chat_id] = task


async def _wake_turn(
    app: Any, session: Any, chat_id: str, turn_driver: Callable[..., Any] | None = None
) -> None:
    """Drive one wake turn, then look again for presses that landed during it.

    The pending flag clears at the start so a burst of clicks schedules one
    wake; the ones that arrive while it runs see a busy chat and floor
    themselves, which is the right answer because the running turn will drain
    them. A press that lands after the drain would otherwise be stranded, so
    the re-check at the end covers it.
    """
    _pending(session).discard(chat_id)
    chat = getattr(session, "chats", {}).get(chat_id)
    if chat is None:
        return

    breaker = _get_wake_breaker()
    await wake_turn.drive(
        app,
        session,
        chat_id,
        breaker=breaker,
        body=_WAKE_NUDGE,
        error_label="card wake turn ended in a swallowed stream_error",
        runtime_origin="card_press",
        turn_driver=turn_driver,
    )

    if (
        _chat_idle(session, chat_id)
        and chat.has_pending_card_presses()
        and chat_id not in _pending(session)
        # Last, because it records what it refuses.
        and breaker.allow(subject=f"further card presses in chat {chat_id}")
    ):
        _pending(session).add(chat_id)
        schedule_wake(app, session, chat_id, turn_driver)


__all__ = ["install", "on_card_press", "find_chat", "reset_wake_breaker"]
