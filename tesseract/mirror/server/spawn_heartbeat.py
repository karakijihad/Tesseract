"""Nothing runs unwatched. A spawn that is still going says so.

Every path that reaches the assistant about a background spawn is
completion-shaped: `spawn_wake` fires when one finishes, and the stall sweep
(`ChatSession._sweep_stalled_spawns`) runs inside a turn, so it only ever
speaks when a turn was already happening. On 2026-08-21 that left eighteen
minutes in which a delegate ran, nothing finished, no turn started, and nothing
said "still no result, go look".

This is the missing trigger, and it is only a trigger. What to do once awake is
already built: `spawn_check` reads status, `spawn_await` blocks for the result,
`work_send` steers, `spawn_cancel` stops. All four were reachable that evening
and three went unused because no turn ever started.

**The shape is `spawn_wake`'s and the gates are its gates.** A completion
arriving and a spawn taking too long are the same problem, so this reuses the
same idle check, the same per-chat pending flag, and the same `spawn-wake`
circuit breaker. Sharing the pending flag is what stops a heartbeat and a
completion driving one chat at once; sharing the breaker is what stops a
trigger that can fire repeatedly from spinning a chat in front of the operator.

**A chat is watched because it was wired, not because it was found.**
`spawn_wake.wire_chat` runs for every surface that can take a wake turn, the
cockpit and the channels alike, and it already holds the four things this needs:
the session, the chat id, the registry, and the turn driver that surface wakes
through. Registering there rather than scanning the app is what keeps the two
surfaces on one funnel, and a chat that was never wired has no wake path to
give it anyway.

**The report names what is running and for how long**, because the assistant's
job on waking is to choose among those four verbs, and a bare "something is
running" reproduces the failure this exists to stop.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_spawn_heartbeat_interval_s,
    load_spawn_heartbeat_seconds,
)
from tesseract.mirror.server import wake_turn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Watch:
    """One wired chat, held weakly so a closed window is not kept alive."""

    session_ref: "weakref.ref[Any]"
    chat_id: str
    turn_driver: Callable[..., Any] | None


#: Keyed by chat id alone. Keying by the session too would leave one entry per
#: session that ever held the chat, and `spawn_wake._another_session_is_turning`
#: says why there can be more than one: every connect builds a fresh
#: `ServerSession` and the predecessor's cleanup is not ordered against it.
#: `wire_chat` runs on every connect and restore, so last write wins names the
#: session that should be woken.
_watched: dict[str, _Watch] = {}

#: Per handle: how many whole thresholds had elapsed when it was last reported.
#: This is what makes the report repeat on a cadence instead of once, and what
#: stops one sweep repeating another's. Pruned to the live handles each sweep.
_reported: dict[str, int] = {}


def watch(
    session: Any, chat_id: str, turn_driver: Callable[..., Any] | None = None
) -> None:
    """Start watching one wired chat. Called from `spawn_wake.wire_chat`."""
    _watched[chat_id] = _Watch(
        session_ref=weakref.ref(session), chat_id=chat_id, turn_driver=turn_driver
    )


def reset() -> None:
    """Test-only: forget every watched chat and every report already made."""
    global _said_down
    _watched.clear()
    _reported.clear()
    _said_down = False


def elapsed_phrase(seconds: float) -> str:
    """How long, in the words a person would use. Minutes below two hours.

    Seconds below a minute, because a threshold short enough to reach that is
    a threshold an operator set on purpose, and "0 minutes" tells them nothing.
    """
    minutes = int(seconds // 60)
    if minutes < 1:
        whole = int(seconds)
        return f"{whole} second{'' if whole == 1 else 's'}"
    if minutes < 120:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"{hours} hours"
    return f"{hours} hours {rest} minute{'' if rest == 1 else 's'}"


def _handle_line(handle: Any, seconds: float, now: datetime | None = None) -> str:
    """One running spawn, as the wake turn is told about it.

    Says how long it has been quiet as well as how long it has been going,
    because by the time this is written the spawn has passed the freshness
    gate in `_overdue` and the silence is the news. Stated as a fact and not
    as a diagnosis: a long compile is quiet and fine, and only the assistant
    holding the task can tell that from a hang.
    """
    from tesseract.brain.spawns import _bounded_one_line, handle_quiet_seconds

    goal = _bounded_one_line(getattr(handle, "goal", None))
    named = f'"{goal}" ' if goal else ""
    quiet = handle_quiet_seconds(handle, now or datetime.now(timezone.utc))
    silence = (
        f" Nothing has come out of it for {elapsed_phrase(quiet)}."
        if quiet is not None
        else ""
    )
    return (
        f"- {named}({getattr(handle, 'kind', '?')}, handle "
        f"{getattr(handle, 'handle_id', '?')}) has been running for "
        f"{elapsed_phrase(seconds)}.{silence}"
    )


def nudge_text(lines: list[str]) -> str:
    """The wake turn's body: what is still running, then the decision.

    The four verbs are named because they are the whole point of waking. An
    assistant told only that time has passed has nothing to act on.
    """
    running = "\n".join(lines)
    return (
        "(Background work you started is still running and nothing has come "
        "back from it yet.\n"
        f"{running}\n"
        "Decide whether to keep waiting, check on it with spawn_check, steer "
        "it with work_send, or stop it with spawn_cancel. Say what you chose "
        "and why.)"
    )


def _running_handles(cs: Any) -> list[Any]:
    """The spawns this chat is waiting on, as the registry itself defines them.

    `SpawnRegistry.list_running` is the one definition, shared with the
    per-turn halt watchdog, so the two cannot disagree about which handles are
    still going or about the parked ones both leave alone.
    """
    spawns = getattr(cs, "spawns", None)
    if spawns is None:
        return []
    try:
        return list(spawns.list_running())
    except Exception:  # noqa: BLE001 — a bare registry double is not a reason to stop
        return []


def _overdue(
    handles: list[Any], threshold: float, now: datetime
) -> list[tuple[Any, float, int]]:
    """Each handle due to be reported again, with its elapsed seconds and the
    multiple of the threshold it has reached.

    **The reminder backs off.** A handle is reported at one threshold, then at
    two, four, eight and so on, rather than at every multiple. Reporting at
    every one means sixty wake turns for a spawn that runs ten hours at the
    shipped threshold, which is a nag with a bill attached; reporting once
    leaves a long run silent after its first ten minutes. Doubling keeps it
    watched for as long as it runs and costs six turns instead of sixty.

    **Elapsed time alone is not a reason to speak.** A spawn that emitted
    something within the threshold is visibly progressing, and waking the
    assistant about it produces the only sentence there is to write: still
    running, nothing wrong. Five of those reached the operator on 2026-09-03.
    So a handle that reports its own activity is skipped while that activity
    is fresh, and reported the moment it goes quiet, which is when the four
    verbs mean anything. A handle that reports NO activity is never skipped:
    `handle_quiet_seconds` returns `None` for a substrate that cannot answer,
    and reading that as fresh would silence the report altogether. Skipping
    leaves `_reported` untouched, so the next sweep offers the handle again
    rather than the silence being spent.
    """
    from tesseract.brain.spawns import handle_age_seconds, handle_quiet_seconds

    out: list[tuple[Any, float, int]] = []
    for handle in handles:
        seconds = handle_age_seconds(handle, now)
        if seconds is None:
            continue
        quiet = handle_quiet_seconds(handle, now)
        if quiet is not None and quiet < threshold:
            continue
        multiple = int(seconds // threshold)
        if multiple < 1:
            continue
        last = _reported.get(handle.handle_id, 0)
        if multiple < max(1, last * 2):
            continue
        out.append((handle, seconds, multiple))
    return out


#: Whether the operator has already been told, this trip, that proactive
#: waking is off. Cleared when the breaker closes again, so a second outage
#: says so a second time and one outage does not say it every pass.
_said_down = False


def _say_the_watchdog_is_down(count: int, retry_in: float | None) -> None:
    """Tell the operator the difference between quiet and broken.

    A tripped breaker suppresses every proactive wake in the process, so the
    chat that would have been woken simply stays silent, which is exactly what
    it looks like when there is nothing to report. This logger is on the pulse
    forwarder's elevated list, so one warning reaches the operator's screen.

    The remedy is the breaker's own: it tries again when its wait is up, so the
    sentence says when rather than asking for anything to be done.
    """
    global _said_down
    if _said_down:
        return
    _said_down = True
    recovery = (
        f"It tries again by itself in about {elapsed_phrase(retry_in)}."
        if retry_in
        else "It stays off until it is reset."
    )
    logger.warning(
        "Background work is running and nothing will interrupt you about it: "
        "%d task(s) are past their reminder and proactive waking has stopped "
        "itself after repeated failed turns. %s Results are not lost and still "
        "arrive when you next speak.",
        count,
        recovery,
    )


def _clear_the_watchdog_notice() -> None:
    global _said_down
    _said_down = False


def _owes_a_result(cs: Any) -> bool:
    """True when a finished spawn is queued on this chat and nobody has read it.

    The completion path wakes a chat when a spawn finishes AND the chat is idle
    at that moment. Both of the failures this phase was built from were the
    other case: on 2026-08-21 the result landed while a turn was in flight, so
    the wake was ineligible by design and the floor queued it for a next turn
    that never came; on 2026-08-24 a restored chat replayed an undelivered
    record into its queue, where nothing starts a turn at all.

    So the heartbeat looks for both. A spawn that is still going, and a result
    that is already back and unread. Reporting "still running" was never the
    whole promise: nothing runs unwatched means nothing finishes unread either.
    """
    try:
        return bool(cs.has_pending_spawn_completions())
    except Exception:  # noqa: BLE001 — a double without the method is not a reason to stop
        return False


def _reachable(app: Any, session: Any) -> bool:
    """Whether a turn started on this session could still reach anyone.

    A cockpit session whose window has gone is not merely idle, it is
    unreachable, and it is not collected either: `SpawnOwnershipIndex` holds a
    strong reference to the session that started a still-running spawn until
    the operator reconnects, so the weak reference above stays alive and the
    chat's registry goes on truthfully reporting the handle. Without this gate
    the heartbeat would drive a paid turn at a closed socket on every pass for
    as long as the spawn ran, and each of those failures counts against the
    breaker that guards every other chat.

    A channel session is reachable by definition and must not be measured this
    way: it is deliberately absent from `server_sessions` because it is
    headless, and its wake is delivered by the bridge rather than a socket.
    That is a difference in what the transport carries, which is the only
    difference a surface is allowed.
    """
    if getattr(session, "kind", "cockpit") == "channel":
        return True
    if app is None:
        return True
    from tesseract.mirror.server.spawn_ownership import _is_connected

    return _is_connected(app, session)


def sweep(app: Any, *, now: datetime | None = None) -> int:
    """One pass. Returns how many chats were woken.

    Reads the threshold on every pass so a changed value takes effect without
    a restart, and returns immediately at `0`, which is the rollback: with the
    heartbeat off, the completion path is untouched and behaves exactly as it
    did before this module existed.
    """
    threshold = load_spawn_heartbeat_seconds(default_runtime_config_path())
    if threshold <= 0:
        return 0
    from tesseract.mirror.server import spawn_wake

    # `is_open`, not `is_tripped`: this asks whether the outage is over, and a
    # breaker whose cooldown has elapsed is willing to try again but has not
    # recovered yet. Clearing on the probe window would tell the operator it
    # was back every cooldown, all through one outage.
    if not spawn_wake._get_wake_breaker().is_open:
        _clear_the_watchdog_notice()

    ref = now or datetime.now(timezone.utc)
    live: set[str] = set()
    woken = 0
    for key, entry in list(_watched.items()):
        session = entry.session_ref()
        if session is None:
            del _watched[key]
            continue
        cs = getattr(session, "chats", {}).get(entry.chat_id)
        if cs is None:
            del _watched[key]
            continue
        handles = _running_handles(cs)
        live.update(h.handle_id for h in handles)
        overdue = _overdue(handles, threshold, ref)
        owed = _owes_a_result(cs)
        if not overdue and not owed:
            continue
        if not _reachable(app, session):
            continue
        # The gates in the order `spawn_wake` applies them. A chat that fails
        # one keeps its handles unreported, so the next sweep offers them
        # again rather than swallowing the report.
        if not spawn_wake.chat_idle(session, entry.chat_id):
            continue
        if entry.chat_id in session.spawn_wake_pending:
            continue
        breaker = spawn_wake._get_wake_breaker()
        if not breaker.allow(subject=f"overdue work in chat {entry.chat_id}"):
            _say_the_watchdog_is_down(len(overdue), breaker.remaining_cooldown())
            continue
        if spawn_wake._another_session_is_turning(app, session, entry.chat_id):
            continue
        previous = {h.handle_id: _reported.get(h.handle_id, 0) for h, _s, _m in overdue}
        for handle, _seconds, multiple in overdue:
            _reported[handle.handle_id] = multiple
        session.spawn_wake_pending.add(entry.chat_id)
        if overdue:
            schedule_wake(
                app,
                session,
                entry.chat_id,
                [_handle_line(h, s, ref) for h, s, _m in overdue],
                entry.turn_driver,
                previous,
            )
        else:
            # A result is already sitting in the chat's queue. The completion
            # path composes that turn, so this only starts one.
            spawn_wake.schedule_wake(app, session, entry.chat_id, entry.turn_driver)
        woken += 1
    for handle_id in [hid for hid in _reported if hid not in live]:
        del _reported[handle_id]
    return woken


def schedule_wake(
    app: Any,
    session: Any,
    chat_id: str,
    lines: list[str],
    turn_driver: Callable[..., Any] | None = None,
    previous: dict[str, int] | None = None,
) -> None:
    """Start the wake turn and register it as the chat's in-flight turn."""
    from tesseract.mirror.server.ws import _spawn_tracked

    task = _spawn_tracked(
        app,
        _wake_turn(app, session, chat_id, lines, turn_driver, previous),
        f"spawn_heartbeat:{getattr(session, 'session_id', '?')}:{chat_id}",
    )
    session.current_turn_tasks[chat_id] = task


async def _wake_turn(
    app: Any,
    session: Any,
    chat_id: str,
    lines: list[str],
    turn_driver: Callable[..., Any] | None = None,
    previous: dict[str, int] | None = None,
) -> None:
    """Drive one heartbeat turn, then give the report back if it was stopped.

    The turn itself and its breaker accounting are `wake_turn.drive`, shared
    with the completion wake and the card wake.

    **The body is stashed rather than passed**, because the channel driver
    takes no body: it composes one by calling `spawn_wake.wake_nudge_text`,
    which describes a completion. A heartbeat that let it do that told the
    operator a still-running spawn had finished. Stashing puts the same
    sentence in front of both surfaces through the one function they share.
    """
    from tesseract.mirror.server import spawn_wake

    session.spawn_wake_pending.discard(chat_id)
    if getattr(session, "chats", {}).get(chat_id) is None:
        return

    body = nudge_text(lines)
    if turn_driver is not None:
        # Only the channel path needs the stash, and stashing on the cockpit
        # path would leave a heartbeat sentence sitting in the slot for the
        # next completion wake on this chat to pop.
        spawn_wake.stash_wake_body(session, chat_id, body)
    outcome = await wake_turn.drive(
        app,
        session,
        chat_id,
        breaker=spawn_wake._get_wake_breaker(),
        body=body,
        error_label="heartbeat turn ended in a swallowed stream_error",
        runtime_origin="spawn_stalled",
        turn_driver=turn_driver,
    )

    if outcome.cancelled:
        # The operator stopped it. Neither a failure nor a success, and the
        # report it was carrying was never read: put the handles back where
        # they were so the next sweep offers them again. Without this the
        # backoff would carry a cancelled turn's silence all the way to the
        # next doubling.
        for handle_id, multiple in (previous or {}).items():
            if multiple:
                _reported[handle_id] = multiple
            else:
                _reported.pop(handle_id, None)


async def heartbeat_loop(app: Any) -> None:
    """Look on the configured interval, for the life of the backend.

    Both config reads are inside the same guard, and neither can end the loop.
    Nothing restarts this task once its coroutine returns: it is created once
    at boot and touched again only to be cancelled at shutdown, so a `return`
    here means the heartbeat is gone for the life of the process. A file being
    rewritten as the loop reads it, or an operator halfway through an edit, is
    a reason to keep the last good interval and look again, not a reason for
    the thing that watches everything else to stop watching.
    """
    # Read once here, outside the guard, so a genuinely missing or malformed
    # key stops the substrate loudly at boot with the substrate's own degrade
    # line, rather than running on a default this file invented.
    interval = load_spawn_heartbeat_interval_s(default_runtime_config_path())
    while True:
        try:
            sweep(app)
            interval = load_spawn_heartbeat_interval_s(default_runtime_config_path())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad pass must not end the loop
            logger.exception(
                "spawn heartbeat: pass failed, looking again in %.0fs", interval
            )
        await asyncio.sleep(interval)


__all__ = [
    "elapsed_phrase",
    "heartbeat_loop",
    "nudge_text",
    "reset",
    "sweep",
    "watch",
]
