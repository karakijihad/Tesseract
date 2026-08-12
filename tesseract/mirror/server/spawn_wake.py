"""Spawn push-on-completion Stage 2 — idle-wake autonomous turn.

Stage 1 (floor) surfaces a finished background spawn at the assistant's *next* turn:
the completion is queued by ``ChatSession.ingest_spawn_completion`` and drained
into the turn's iteration-0 injection. This module adds the proactive half —
when a spawn finishes while the owning chat is **idle** (no turn in flight),
start a turn so the assistant reads the completion and acts on its own, instead of
waiting for the operator's next message.


The wake turn carries only a short nudge body; the ``[spawn_completed]`` detail
rides in via Stage 1's injection (single source — no double-surface). Wiring is
Mirror-only: the REPL keeps the bare floor notifier (no turn-driver to wake).

ws.py imports are deferred inside the scheduler so the decision path
(:func:`on_spawn_complete`) stays import-light and unit-testable without the
backend, and to avoid a ws ↔ spawn_wake import cycle.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from tesseract.context.circuit_breaker import CircuitBreaker
from tesseract.paths import TESSERACT_HOME, log_dir

logger = logging.getLogger(__name__)

# transient=False (a real chat turn) so the assistant's proactive reaction is visible in
# the owning chat; the completion detail is already noted above via Stage 1.
_WAKE_NUDGE = (
    "(A background task you started has finished — its result is noted above. "
    "Decide whether to act on it.)"
)

# Session-scoped dict (chat_id -> label), stashed by on_spawn_complete for the
# handle that actually triggers the wake and popped by wake_nudge_text() once
# consumed — a plain getattr/setattr slot (not a dataclass field) so fake
# session doubles across the test suite keep working unmodified.
_WAKE_LABEL_ATTR = "_spawn_wake_labels"


def _handle_label(handle: Any) -> str | None:
    """Task 6.3 — name the workstream in the wake note the same way the
    Activity registry already names it (Task 6.1, brain/spawns.py::
    _spawn_record): the spawn's goal snippet, falling back to its kind."""
    from tesseract.brain.spawns import _bounded_one_line

    return _bounded_one_line(getattr(handle, "goal", None)) or getattr(handle, "kind", None)


def _stash_wake_label(session: Any, chat_id: str, handle: Any) -> None:
    label = _handle_label(handle)
    labels: dict[str, str] | None = getattr(session, _WAKE_LABEL_ATTR, None)
    if labels is None:
        labels = {}
        setattr(session, _WAKE_LABEL_ATTR, labels)
    if label:
        labels[chat_id] = label
    else:
        # A label-less completion must not let a previous workstream's stale
        # label leak into its wake nudge.
        labels.pop(chat_id, None)


def wake_nudge_text(session: Any, chat_id: str) -> str:
    """Render the wake-turn nudge, naming the workstream (Task 6.3) whose
    completion triggered this wake. Falls back to the bare ``_WAKE_NUDGE``
    text when no label was stashed (e.g. a bare ``_wake_turn`` call in tests,
    or a handle with neither goal nor kind) — same wording as before this
    change, so callers that never stash a label are unaffected. Shared by
    both the cockpit driver (``_wake_turn``) and the channel driver
    (``integrations/telegram/bridge.py::_wake_turn_driver``) so the two
    delivery legs never drift apart.
    """
    labels: dict[str, str] | None = getattr(session, _WAKE_LABEL_ATTR, None)
    label = labels.pop(chat_id, None) if labels else None
    if not label:
        return _WAKE_NUDGE
    return (
        f'(A background task you started — "{label}" — has finished; its '
        "result is noted above. Decide whether to act on it.)"
    )

# G1 (idle-wake-design.md) — one global breaker, not per-chat/per-session
# (a per-session breaker would leak one JSONL per session id). Threshold comes
# from the shared `CircuitBreaker` default (providers.yaml::availability.
# max_consecutive_failures) — never a literal here.
_WAKE_BREAKER_NAME = "spawn-wake"
_wake_breaker: CircuitBreaker | None = None


def _wake_breaker_log_dir() -> Path:
    """Resolve the circuit-breaker log dir at call time (never an import-time
    constant) so a test's `monkeypatch.setenv("TESSERACT_HOME", tmp_path)`
    lands the JSONL under its own tmp dir (kernel/workspace_changes.py::
    workspace_events_dir idiom)."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return log_dir("circuit-breakers")


def _get_wake_breaker() -> CircuitBreaker:
    """Lazily construct the process-global spawn-wake breaker singleton."""
    global _wake_breaker
    if _wake_breaker is None:
        _wake_breaker = CircuitBreaker(
            name=_WAKE_BREAKER_NAME, log_dir=_wake_breaker_log_dir(),
        )
    return _wake_breaker


def chat_idle(session: Any, chat_id: str) -> bool:
    """True when ``chat_id`` has no turn in flight (eligible for a wake)."""
    task = session.current_turn_tasks.get(chat_id)
    return task is None or task.done()


def on_spawn_complete(
    app: Any,
    session: Any,
    cs: Any,
    chat_id: str,
    handle: Any,
    floor: Callable[[Any], None],
    turn_driver: Callable[..., Any] | None = None,
) -> None:
    """Notifier override fired from a spawn's done-callback (Mirror only).

    Always runs Stage 1's floor (queue the ``[spawn_completed]`` note). Then, if
    the owning chat is idle, no wake is already pending, and the shared
    ``spawn-wake`` breaker is closed, schedules exactly one wake turn. When a
    turn is already in flight the floor is enough — the running turn drains
    the note at its next tool boundary / iteration 0. When the breaker is
    open, the wake is skipped (the floor note still surfaces at the chat's
    next operator turn — nothing is lost, only proactivity).

    ``turn_driver``: optional turn-driver override threaded through to
    :func:`schedule_wake` / :func:`_wake_turn` — ``None`` (default) drives
    the cockpit path (``_run_chat_turn``); channel sessions (Telegram) pass
    a channel-shaped driver so wake output flows through the bridge's real
    send path instead.
    """
    floor(handle)
    # Stash before the idle/pending/breaker gates, not after: a straggler
    # completing while a wake turn is in flight re-schedules from
    # `_wake_turn`, which has no handle to derive a label from — the label
    # stashed here is what that re-scheduled wake's nudge pops (Deferred
    # 2026-07-12, straggler mid-wake label gap). Last completion wins.
    _stash_wake_label(session, chat_id, handle)
    if not chat_idle(session, chat_id):
        return
    if chat_id in session.spawn_wake_pending:
        return
    if _get_wake_breaker().is_tripped:
        return
    session.spawn_wake_pending.add(chat_id)
    if turn_driver is None:
        schedule_wake(app, session, chat_id)
    else:
        schedule_wake(app, session, chat_id, turn_driver)


def schedule_wake(
    app: Any, session: Any, chat_id: str, turn_driver: Callable[..., Any] | None = None,
) -> None:
    """Spawn the wake turn task and register it as the chat's in-flight turn."""
    from tesseract.mirror.server.ws import _spawn_tracked

    task = _spawn_tracked(
        app,
        _wake_turn(app, session, chat_id, turn_driver),
        f"spawn_wake:{session.session_id}:{chat_id}",
    )
    session.current_turn_tasks[chat_id] = task


async def _wake_turn(
    app: Any, session: Any, chat_id: str, turn_driver: Callable[..., Any] | None = None,
) -> None:
    """Drive one wake turn on the owning chat, then re-check for stragglers.

    Clears the pending flag at start so a burst of completions schedules one
    wake (later completions now see the chat busy and floor-ingest). After the
    turn, if a completion landed mid-wake (after the iteration-0 drain) and the
    chat is idle again, schedules one more wake so the note isn't stranded.

    G1 (fix pass 1): the shared ``spawn-wake`` breaker records the ACTUAL turn
    outcome, not just exceptions — both drivers can swallow an ordinary turn
    failure internally without raising (cockpit ``ws._run_turn``'s
    ``stream_error`` envelope; channel ``ws._start_channel_turn``'s
    ``error_holder``), so a pure except-Exception accounting would silently
    never count these failures. The cockpit path threads an ``outcome`` dict
    through ``_run_chat_turn``/``_run_turn`` (``{"ok": stream_ok}``); a
    channel-shaped ``turn_driver`` instead returns ``str | None`` — a non-
    ``None`` string is the observed turn-level error. Either signal ->
    ``record_failure``; a clean turn -> ``record_success``. An exception still
    propagating out of the driver remains the backstop path (``record_failure``
    then re-raise, unchanged from before).

    G1 (fix pass 2, 2026-07-06): an operator-cancelled cockpit wake turn is
    NEITHER a failure nor a success — ``ws._run_turn`` now also sets
    ``outcome["cancelled"]`` in its CancelledError branch, and a cancelled
    cockpit turn here skips both ``record_failure`` and ``record_success``
    (breaker state untouched). Channel behavior is unchanged: a cancelled
    channel wake turn still returns ``None`` from ``_start_channel_turn``
    (its own CancelledError handling) and is recorded as a clean success,
    same as before this fix.
    """
    session.spawn_wake_pending.discard(chat_id)
    cs = session.chats.get(chat_id)
    if cs is None:
        return

    breaker = _get_wake_breaker()
    turn_error: str | None = None
    turn_cancelled = False
    # Whether the turn actually delivered what it drained. A turn can end
    # perfectly well and still not commit — an adapter error it recovered from
    # means the retry no longer carried the block. That is the difference
    # between "try again" and "trying again just repeats it".
    delivery_committed = True
    try:
        if turn_driver is None:
            from tesseract.mirror.server.turn_runner import _run_chat_turn

            outcome: dict[str, Any] = {}
            await _run_chat_turn(
                app, session, wake_nudge_text(session, chat_id), chat_id=chat_id, outcome=outcome,
            )
            delivery_committed = bool(outcome.get("committed", True))
            if outcome.get("cancelled"):
                turn_cancelled = True
            elif outcome.get("ok") is False:
                turn_error = "cockpit wake turn ended in a swallowed stream_error"
        else:
            turn_error = await turn_driver(app, session, chat_id)
            delivery_committed = turn_error is None
    except Exception as exc:
        breaker.record_failure(str(exc))
        raise
    if turn_cancelled:
        pass  # neutral — breaker state untouched
    elif turn_error:
        breaker.record_failure(turn_error)
    else:
        breaker.record_success()

    # This straggler re-schedule used to be unreachable after a failed turn —
    # the drain consumed the note whatever the outcome, so nothing was left
    # pending to re-wake on. The delivery cursor changed that: a turn that does
    # not commit rolls the note back, and re-waking on the note this turn just
    # failed to deliver is a loop, not a retry. Both gates are needed, and the
    # breaker alone is not enough: a turn that errors and RECOVERS ends clean,
    # so the breaker records a success and never trips while the delivery keeps
    # rolling back. Re-wake only for a straggler that arrived during a turn
    # which did deliver. Nothing is lost either way — the note survives in the
    # queue for the operator's next turn, and in the store across a restart.
    if (
        chat_idle(session, chat_id)
        and cs.has_pending_spawn_completions()
        and delivery_committed
        and not breaker.is_tripped
    ):
        if chat_id not in session.spawn_wake_pending:
            session.spawn_wake_pending.add(chat_id)
            if turn_driver is None:
                schedule_wake(app, session, chat_id)
            else:
                schedule_wake(app, session, chat_id, turn_driver)


def _build_notifier(
    app: Any, session: Any, chat_id: str, cs: Any,
    turn_driver: Callable[..., Any] | None = None,
) -> Callable[[Any], None]:
    """Build the completion-notifier closure for ``(session, chat_id, cs)``.

    Extracted from :func:`wire_chat` (M4-p2) so `spawn_ownership.rebind_chat`
    can install the SAME notifier shape onto a DIFFERENT (stale, orphaned)
    registry at reconnect time — not just onto ``cs.spawns`` itself.

    The floor folds a completion into ``cs`` via `spawn_ownership.
    deliver_once` (app-aware dedup + hygiene; degrades to the bare
    ``cs.ingest_spawn_completion`` call when ``app`` is ``None``, e.g. the
    bare-registry unit tests). The idle-wake override sits on top, unchanged.
    """
    from tesseract.mirror.server.spawn_ownership import deliver_once

    def floor(handle: Any) -> None:
        deliver_once(app, session, chat_id, cs, handle)

    def _notifier(handle: Any) -> None:
        on_spawn_complete(app, session, cs, chat_id, handle, floor, turn_driver)

    return _notifier


def wire_chat(
    app: Any, session: Any, chat_id: str, cs: Any,
    turn_driver: Callable[..., Any] | None = None,
) -> None:
    """Override one chat's spawn completion notifier to also schedule a wake,
    and record spawn-start ownership for reconnect rebind (M4-p2).

    The floor (``cs.ingest_spawn_completion``, folded through `spawn_ownership.
    deliver_once`) always runs; the override adds the idle-wake on top.
    Re-wiring is safe — the floor is rebuilt fresh each call, not the
    previously-installed wrapper.

    Also points ``cs.spawns.on_register`` at `spawn_ownership.register_owner`
    so any spawn started under this chat is recorded in the app-level
    `SpawnOwnershipIndex` the moment it registers — the reconnect rebind
    needs that mapping to find still-running handles by chat_id, not just
    completed ones. Skipped for channel (Telegram/etc.) sessions: they are
    deliberately never added to ``app["server_sessions"]`` (headless, no
    cockpit WS — see `integrations/telegram/bridge.py`), so
    `spawn_ownership._is_connected` never sees them as connected, their
    entries would never finalize, and `rebind_chat` (Mirror-WS-reconnect
    only) never visits them either — tracking would only leak, never help.

    ``turn_driver``: see :func:`on_spawn_complete` — forwarded unchanged so a
    channel session's wake turns route through its channel-shaped driver.
    """
    cs.spawns.completion_notifier = _build_notifier(app, session, chat_id, cs, turn_driver)
    if getattr(session, "kind", "cockpit") != "channel":
        from tesseract.mirror.server.spawn_ownership import register_owner

        # The registry only learns which chat owns it here — and that is the
        # key its completions are recorded under, so a result outlives the
        # process (`brain/completion_store.py`). Skipped for channel sessions
        # for the same reason ownership tracking is: a headless session mints
        # a fresh `active_chat_id` on every rebuild and has no restore path to
        # replay into, so a record written under one could only ever leak.
        cs.spawns.chat_id = chat_id

        cs.spawns.on_register = lambda handle: register_owner(
            app, session, cs, chat_id, handle.handle_id,
        )


def install(app: Any, session: Any) -> None:
    """Wire idle-wake on every currently-open chat of ``session`` (at connect).

    Chats created later (``chat.create``) are wired by the handler via
    :func:`wire_chat`.
    """
    for chat_id, cs in session.chats.items():
        wire_chat(app, session, chat_id, cs)


def _another_session_is_turning(app: Any, session: Any, chat_id: str) -> bool:
    """True when some OTHER live session already has a turn in flight for this
    chat.

    Every WS connect builds a fresh ``ServerSession`` with empty
    ``current_turn_tasks``, and the predecessor's cleanup is not ordered
    against it — so on a fast reload the new session's idle check would say
    "idle" while the old session's wake turn is still streaming, and both
    would drive the same chat. Nothing is lost (the durable claim is chat-keyed
    and idempotent) but it is a duplicate inference the operator pays for.
    """
    if app is None:
        return False
    try:
        sessions = (app.get("server_sessions") or {}).values()
    except Exception:  # noqa: BLE001 — a stub app is not a reason to skip
        return False
    for other in sessions:
        if other is session:
            continue
        task = getattr(other, "current_turn_tasks", {}).get(chat_id)
        if task is not None and not task.done():
            return True
    return False


def reconcile_on_connect(app: Any, session: Any) -> int:
    """Wake any chat that came back owing a result. Returns how many.

    :func:`on_spawn_complete` covers the completion that lands while the
    backend is up. This covers the other half: one that landed while it was
    not. The durable record is replayed into the rebuilt chat at restore
    (`ChatSession.replay_undelivered_completions`), and without this the
    operator would have to speak before the assistant read a result he already has —
    which is most of the value of making it durable at all.

    Runs at connect, after :func:`install`, so the notifier is wired before a
    wake turn can start. Same three gates as the live path: the chat must be
    idle, must not already have a wake pending, and the shared breaker must be
    closed. A tripped breaker skips the sweep entirely rather than per chat —
    the failure it is counting is not chat-specific.
    """
    if _get_wake_breaker().is_tripped:
        return 0
    woken = 0
    for chat_id, cs in list(session.chats.items()):
        if not cs.has_pending_spawn_completions():
            continue
        if not chat_idle(session, chat_id):
            continue
        if chat_id in session.spawn_wake_pending:
            continue
        if _another_session_is_turning(app, session, chat_id):
            continue
        session.spawn_wake_pending.add(chat_id)
        schedule_wake(app, session, chat_id)
        woken += 1
    return woken
