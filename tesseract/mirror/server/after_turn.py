"""The consolidation boundary, once, wherever the turn came from.

Compaction is one moment in the runtime and it had two implementations: the
cockpit's in `turn_runner`, a channel's in `integrations/_channel_session`.
They drifted the way two implementations of one moment always do. A channel
had no compaction at all for months, then had it without telling anyone, then
told the operator but never appeared in the log the cockpit writes and never
touched the counter the cockpit keeps.

So the DECISION and the BOOKKEEPING live here and are the same for everyone.
Only DELIVERY is injected, because delivery is the one thing a surface can
genuinely differ in: the cockpit has a context meter and a WebSocket to move
it, a phone has a line of text, and a future email lane has neither.

The caller passes what its transport can carry and nothing else. Anything a
caller could get wrong by passing it is not a parameter.

## One pipeline

There is one consolidation and it always reflects. Two things can start it and
they are recorded, never branched on:

    SOFT   the turn judged, through `session_continue`
    HARD   the window filled, through `should_compact()`

and it ends in one of two answers, both of which the AGENT gives:

    CONTINUE   reflect, then rebuild the room and keep working
    RESET      reflect, then leave the conversation behind

Carrying on is the third answer and it is not a mode: nobody asked and the
window has room, so this function returns having done nothing.

The runtime owns when a hard boundary is mandatory. It never chooses between
continue and reset. Where a hard boundary is reached and no answer can be had
-- which is every hard boundary until the reflection call carries the answer
back with its deltas -- it CONTINUES. Losing the room is recoverable; ending a
conversation the operator was in the middle of is not.

## One act, and the only difference is what follows it

Both answers do the same four things: reflect on a snapshot, archive what was
said, clear the conversation in place, keep the thread. Then a CONTINUE hands
the cleared conversation the package `brain/continuity.py` renders from the
checkpoint, and a RESET hands it nothing. There is no second path and nothing
branches on the trigger.

Clearing in place rather than opening a new conversation is the operator's
reading, 2026-09-06, and it is what the document asks for: a fresh CONTEXT,
which is an empty payload and the cost floor back at zero, not a fresh chat
record. The head does not move across the boundary, so the cached prefix
survives it.

The package cannot be built here. Reflection is a model turn running in the
background by design, and the checkpoint it writes does not exist until it
finishes, which is after this function has returned. So the delivery rides the
reflection's own completion callback, and if the operator speaks first it
arrives after their first reply.

There is no fallback. A consolidation that could not clear leaves the
conversation STANDING, with the boundary still owed so the next turn retries,
and says so at ERROR. There used to be a fold here, a second mechanism
answering the same question with a different act, and on its worst branch it
summarised the middle away immediately after the runtime had failed to archive
a copy of it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from tesseract.brain.chat import Continuation
from tesseract.orchestrator.autonomy import journal
from tesseract.memory.log_notes import append_log_entry
from tesseract.paths import log_dir

log = logging.getLogger(__name__)

#: What a person is told when the turn decided it was finished with this
#: conversation. It lives beside the moment that fires it so there is one
#: sentence and not one per surface.
RESET_NOTICE = (
    "That work is done, so I am starting fresh here to keep this quick. I am "
    "writing down what it taught me. Ask me about any of it and I will read it "
    "back from the record."
)


#: What a person is told when the runtime stopped a conversation that kept
#: deciding to carry on. NOT `RESET_NOTICE`: that one says the work is done,
#: and here it is not — the runtime stopped it, and saying otherwise would
#: hide the only thing about this worth knowing. The reason is filled in.
REFUSED_NOTICE = (
    "I was going to carry this work into a fresh start, and I am stopping "
    "instead: {reason}. What this taught me is written down. Tell me what you "
    "want done next and I will pick it up from there."
)


def _is_a_chat_on_a_channel(channel: str | None, chat_id: str | None) -> bool:
    """Whether these two name a conversation on a channel.

    `is None`, not truthiness. The function this replaced tested for None, and
    a refactor that quietly starts treating an empty string as absent has
    changed behaviour while claiming not to.
    """
    return channel is not None and chat_id is not None


def _label(session: Any, channel: str | None, chat_id: str | None) -> str:
    """What the logs call this conversation."""
    if _is_a_chat_on_a_channel(channel, chat_id):
        return f"{channel}/{chat_id}"
    return str(getattr(session, "session_id", "") or "unknown")


def _record_boundary(
    session: Any, label: str, *, trigger: str, outcome: str, refused: str
) -> None:
    """The tally and the log line. Both surfaces, always. Never raises.

    `compact_count` feeds the `[session_end]` entry, and the `[boundary]` entry
    is the runtime's own record that a consolidation happened at all. A channel
    was missing from both, so a boundary reached on a phone left no trace
    anywhere a later session could read.

    The channel's tally is write-only for now: `[session_end]` is written by
    `ws_connection._autosave` on WebSocket teardown, which a channel session
    never reaches. The `[boundary]` entry is what actually carries a channel
    boundary into the log, and that one both surfaces share.

    No token counts. A boundary clears the conversation, so "before and after"
    is the whole of it and zero, every time, and a pair of numbers that cannot
    differ is not a measurement. What a later reader needs is why it fired and
    what the agent decided.
    """
    try:
        session.compact_count += 1
    except Exception:
        log.exception("boundary count increment failed for %s", label)
    session_id = str(getattr(session, "session_id", "") or "")
    try:
        now = datetime.now(timezone.utc)
        body = (
            f"Consolidated (session={session_id[:8]}, "
            f"turn={getattr(session, 'turn_count', 0)}). "
            f"Trigger: {trigger}  |  Outcome: {outcome}"
        )
        if refused:
            body += f"  |  Stopped instead: {refused}"
        append_log_entry(
            header=f"## [boundary] Consolidation {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            body=body,
            log_dir=log_dir("sessions"),
            date=now,
        )
    except Exception:
        log.exception("logs/sessions [boundary] append failed for %s", label)


async def after_turn(
    chat_session: Any,
    *,
    app: Any,
    session: Any,
    channel: str | None = None,
    chat_id: str | None = None,
    announce: Callable[[str], Awaitable[None]] | None = None,
    ending: Callable[[Callable[[], bool]], Awaitable[bool]] | None = None,
    mid_turn: bool = False,
) -> None:
    """Run the consolidation boundary for a turn that has just landed, if one
    is due, and tell the person whatever their surface can carry.

    `session` is the `ServerSession` the turn ran under, which both surfaces
    have. It carries the compaction tally. Required, not defaulted: the
    bookkeeping here is the half that is supposed to be the same for everyone,
    and a caller that could omit it is a caller that will, which is exactly how
    a channel came to be missing both the tally and the log entry.

    `channel` / `chat_id` name the conversation for the log line. A channel
    used to hand a `chat_memory` here as well, and its evicted rows were
    forwarded to a second rolling summary that a channel kept and the cockpit
    did not. That summary wrote its own output back into the conversation and
    ended up quoting itself; a channel is the same funnel as the cockpit, so
    it consolidates here and nowhere else.

    `announce` sends one line of text to the person, and a caller whose
    transport has none simply omits it.

    `app` is where the workspace event store lives, which a reflection has to
    reach. Required, and not something a caller can get wrong: both surfaces
    have exactly one and the cockpit's hook was already being handed it.

    `ending` is what this surface does with a conversation the turn has
    finished with. The cockpit copies the transcript into its own archived
    record and clears the SAME chat, keeping its id and its place in the rail;
    a channel has no chat to switch to and wipes in place. Opening a new chat
    is `/reset`, which is a different act. Only the ENDING differs, and a
    caller that has none simply cannot be asked to reset. It returns whether
    the conversation was actually left behind AND the clear reached disk,
    because a cockpit that could not write the transcript refuses rather than
    archiving the wrong thing.

    It is HANDED the reflection rather than following it, because the moment
    reflection may fire is the moment the conversation is certainly being left,
    and only the surface knows where that is: for the cockpit it is after the
    transcript is written, and a reflection fired ahead of a persist that then
    failed would have paid for a model turn and filed a proposal about a
    conversation still going on. For a channel the wipe cannot fail, so it is
    immediate.

    The turn's own decision outranks the threshold. `session_continue` records
    one and this is where it is honoured, because `reset()` rewrites history
    in place and doing that mid-turn discards the assistant message carrying
    the pending `tool_use` block. A turn that decided nothing meets exactly
    the threshold check it met before this existed.

    Never raises. The turn has already landed and been sent, and nothing here
    is allowed to turn a delivered answer into a failed one.
    """
    label = _label(session, channel, chat_id)
    if mid_turn or _still_speaking(chat_session):
        # A turn that outgrew the ceiling while it was still speaking. The only
        # safe answer is to make room; the boundary's act is to CLEAR the
        # conversation, and the conversation is the one currently being spoken.
        #
        # `consolidate_in_place` calls `reset()` on the live `ChatSession`,
        # which empties `self.history` while `send()` is mid-iteration. The
        # user message and every tool_use/tool_result pair this turn has
        # already appended go with it, and the next `_messages_for_turn()`
        # builds from nothing. Safe while a boundary opened a NEW session and
        # left the running one alone; unsafe from the moment it started
        # clearing in place.
        #
        # So a mid-turn call records and returns, and never asks whether a
        # boundary is due. The turn's own end asks that, a few seconds later,
        # with the turn finished and its messages safe.
        # `mid_turn` is what the one caller that can reach this says about
        # itself, and `_still_speaking` is what the conversation says. The
        # second is why this is a guard rather than a convention: a future
        # caller wiring the hook without the keyword is protected anyway, and
        # the defect this closes came from exactly that kind of change.
        #
        # It used to fold here, and folding was the wrong answer twice over. A
        # fold was a model call, and one paid for mid-turn is thrown away by
        # the boundary a few seconds later. Worse, a fold that worked put the
        # conversation back UNDER the trigger, so the end of the turn found
        # nothing due and carried on: the crossing was silently answered by a
        # summary instead of by the boundary, and the work never got a
        # reflection, a checkpoint or a package. Heavy tool work crosses the
        # ceiling mid-turn nearly every time, so that was most crossings.
        #
        # So the turn is let finish, on a ceiling that is no longer in its way,
        # and the crossing is remembered. `_boundary_forced` reads it at the
        # end of the turn and the boundary happens then, once.
        _note_the_ceiling_was_crossed(chat_session, label)
        return

    answer = _requested(chat_session, label)
    soft = answer is not None
    nudge = _take_nudge(chat_session, label)
    if not soft and not _boundary_forced(chat_session, label):
        # Carrying on. Not a mode, not a decision the runtime made: nobody
        # asked for a boundary and the window has room, so there is nothing to
        # do. There is something to record only if the observer had asked for
        # one: a recommendation read and not acted on is the evidence
        # that says whether these are worth making.
        _journal_nudge(nudge, answered=None, label=label)
        return

    if answer is None:
        # A hard boundary the turn did not answer for. CONTINUE, never RESET:
        # the work carrying on is recoverable and ending a conversation the
        # operator was in the middle of is not. CC-11 is what allows an answer
        # before this, by reporting how much room is left every turn.
        answer = Continuation.CONTINUE

    trigger = "soft" if soft else "hard"
    refused = ""
    if answer is Continuation.CONTINUE:
        refused = _why_not_continue(chat_session, label)
        if refused:
            # Continuing because continuing is possible. The work is left
            # behind instead, and the record says WHY rather than reading as
            # an ordinary stop: those two are the events a later reader most
            # needs to tell apart.
            log.info("%s may not carry on: %s", label, refused)
            answer = Continuation.RESET

    _journal_nudge(nudge, answered=answer, label=label)

    if await _consolidate(
        app,
        session,
        chat_session,
        label=label,
        trigger=trigger,
        outcome=answer,
        refused=refused,
        announce=announce,
        ending=ending,
    ):
        _record_boundary(
            session,
            label,
            trigger=trigger,
            outcome=answer.value,
            refused=refused,
        )
        _forget_boundary_owed(chat_session, label)
        return

    # It could not clear, so the conversation STANDS. Nothing rewrites it: the
    # only other act that ever made room here summarised the middle away, and
    # on this exact branch it did so immediately after the runtime had failed
    # to archive a copy of it.
    #
    # The debt is deliberately NOT forgotten. The next turn asks again, and
    # keeps asking, until the boundary can actually run. ERROR rather than
    # warning because `logsetup` turns a backend ERROR into a row the operator
    # sees: a conversation that cannot be bounded is not something to discover
    # from a log file later.
    log.error(
        "%s reached a boundary it could not take, so the conversation stands "
        "and the next turn will try again",
        label,
    )


def _still_speaking(chat_session: Any) -> bool:
    """Whether a turn is running on this conversation right now.

    `ChatSession` clears the flag in `send()`'s `finally`, which runs before
    the caller reaches its own end-of-turn hook, so this is False at the true
    boundary and True from anywhere inside the loop.

    A session that cannot say is not speaking. Sub-agent sessions and test
    doubles are the ordinary case, exactly as in `_requested`.
    """
    return bool(getattr(chat_session, "_turn_active", False))


def _boundary_forced(chat_session: Any, label: str) -> bool:
    """Whether the runtime requires a boundary here, whatever the turn thinks.

    One reason, and it is the runtime's own: the window is full. There was a
    second, the same tool failing its way past a count, and GOVERNANCE §11 is
    what deleted it: a tool fails for a network blip, a bad argument, a rate
    limit or a refusal, and clearing a healthy 50,000-token conversation fixes
    none of them. The streak stays as a SIGNAL: `failures_signal` already
    feeds the autonomy digest and the observer, so the pattern still surfaces
    and the agent may act on it as a soft call.

    Asked before reflection so the snapshot the reflection reads is the
    conversation about to be cleared, rather than what is left of it. That
    ordering is why this is the only place it is read: a helper that checked
    again on its way to clearing would be answering a question this boundary
    has already answered.

    A session that cannot answer has no threshold to cross. Sub-agent sessions
    and test doubles are the ordinary case for that, exactly as in `_requested`.
    """
    # Two reasons now. The first is a crossing that already happened, while
    # the turn was speaking and nothing could be done about it. It is read
    # FIRST and on its own, because the other asks how big the conversation
    # is NOW and that is the wrong question here: the turn was allowed to
    # finish rather than be cut short, so the size it reached is exactly what
    # the boundary is for, and a conversation that grew and then had its own
    # tool results trimmed by the guard could measure under the trigger again.
    if _owes_a_boundary(chat_session):
        log.info("%s owes a boundary from a crossing it made mid-turn", label)
        return True
    should = getattr(chat_session, "should_compact", None)
    if should is None:
        return False
    try:
        return bool(should())
    except Exception:
        log.exception("reading the boundary threshold failed for %s", label)
        return False


def _note_the_ceiling_was_crossed(chat_session: Any, label: str) -> None:
    """Remember the crossing so the end of the turn can answer it.

    Best effort, and loudly so. A session that cannot hold this is a sub-agent
    or a test double, and for those the threshold check at the end of the turn
    is the answer it always was.
    """
    note = getattr(chat_session, "note_grew_past_the_ceiling", None)
    if note is None:
        log.info(
            "%s grew past the ceiling mid-turn and cannot record it, so the "
            "end of the turn will decide on size alone",
            label,
        )
        return
    try:
        first = note()
    except Exception:
        log.exception("could not record the mid-turn crossing for %s", label)
        return
    if first:
        # Once. The hook fires on every tool-loop iteration for the rest of
        # the turn, because the cheap check that reaches it stays true until
        # the boundary happens.
        log.info(
            "%s grew past the ceiling mid-turn; it finishes, then consolidates",
            label,
        )


def _owes_a_boundary(chat_session: Any) -> bool:
    return bool(getattr(chat_session, "owes_a_boundary", False))


def _forget_boundary_owed(chat_session: Any, label: str) -> None:
    """The boundary happened. Called only after `_consolidate` said it did.

    A debt cleared before its remedy ran is a boundary that never happens: the
    next turn finds nothing owed, the conversation carries on over the ceiling,
    and the crossing this bool exists to remember is gone.
    """
    forget = getattr(chat_session, "forget_boundary_owed", None)
    if forget is None:
        return
    try:
        forget()
    except Exception:
        log.exception("could not clear the boundary owed by %s", label)


def _take_nudge(chat_session: Any, label: str) -> Any | None:
    """The observer's recommendation shown this turn, or `None`.

    Read once per turn whether or not a boundary follows, because the row this
    writes is about the recommendation and not about the boundary: leaving it
    unread on a turn that carried on would keep a stale nudge queued into the
    next one.
    """
    take = getattr(chat_session, "take_delivered_nudge", None)
    if take is None:
        return None
    try:
        return take()
    except Exception:
        log.exception("reading the delivered nudge failed for %s", label)
        return None


def _journal_nudge(nudge: Any | None, *, answered: Continuation | None, label: str) -> None:
    """One journal row saying what was recommended and what came of it.

    `answered is None` means it was read and no boundary was taken. That is
    the row worth having: a recommendation nobody acts on is either wrong or
    unread, and both are things the operator can only see if the misses are
    written down beside the hits.

    Never raises. This is bookkeeping at the end of a turn that has already
    been delivered.
    """
    if nudge is None:
        return
    try:
        followed = answered is not None and answered.value == nudge.recommendation
        journal.append(
            "observer_nudge",
            {
                "summary": (
                    f"observer recommended {nudge.recommendation}: {nudge.reason}"
                ),
                "recommendation": nudge.recommendation,
                "reason": nudge.reason,
                "observation_id": nudge.observation_id,
                "answered": answered.value if answered is not None else None,
                "followed": followed,
                # On the row rather than in `reason`: the row is stamped, so
                # the figure cannot be read back later as the state now.
                "context_percent": nudge.context_percent,
                "conversation": label,
            },
        )
    except Exception:
        log.exception("journalling the observer nudge failed for %s", label)


def _requested(chat_session: Any, label: str) -> Continuation | None:
    """What the turn asked for, or `None`. Reading it clears it.

    A session object that cannot answer is not an error: sub-agent sessions and
    test doubles have no continuation to give, and they are the ordinary case
    for everything that is not a conversation with a person in it.
    """
    take = getattr(chat_session, "take_continuation", None)
    if take is None:
        return None
    try:
        raw = take()
    except Exception:
        log.exception("reading the turn's decision failed for %s", label)
        return None
    if not raw:
        return None
    try:
        return Continuation(raw)
    except ValueError:
        # `request_continuation` refuses these, so reaching here means
        # something else wrote the field. Say which, and fall through to the
        # threshold rather than guessing what was meant.
        log.error("turn asked for an unknown continuation %r on %s", raw, label)
        return None


def _why_not_continue(chat_session: Any, label: str) -> str:
    """Why this conversation may not carry the work on again, or `""`.

    Asked of the record, which is the only place the answer exists: the
    boundary being decided has not reflected yet, so what it will report is
    not knowable here. What IS knowable is what the boundaries before it
    reported, and that is what says whether anything is moving.

    A conversation with no durable id has no record and no history to loop in,
    which is a sub-agent or a synthetic turn. It is never refused.
    """
    chat_id = str(
        getattr(getattr(chat_session, "tool_context", None), "chat_id", "") or ""
    )
    if not chat_id:
        return ""
    try:
        from tesseract.brain.continuity import why_not_continue

        return why_not_continue(chat_id)
    except Exception:
        log.exception("reading what %s is owed failed", label)
        return ""


def _reflect(
    app: Any,
    session: Any,
    chat_session: Any,
    *,
    label: str,
    trigger: str = "",
    outcome: str = "",
    refused: str = "",
    announce: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    """Distil what this conversation taught, without changing its shape.

    `announce` is handed on as the delivery for the continuity package, which
    can only be built once this reflection has written its checkpoint. It is
    the same callable the notices go through, because the package is a thing
    the person is told and there is no second way to tell them.
    """
    if app is None:
        log.warning("reflection skipped for %s: no app to reach the inbox with", label)
        return False
    from tesseract.mirror.server.handoff import hand_off

    try:
        return hand_off(
            app,
            session,
            chat_session,
            reason="turn_decision",
            label=label,
            trigger=trigger,
            outcome=outcome,
            refused=refused,
            deliver=announce,
        )
    except Exception:
        log.exception("reflection failed for %s", label)
        return False


async def _consolidate(
    app: Any,
    session: Any,
    chat_session: Any,
    *,
    label: str,
    trigger: str,
    outcome: "Continuation",
    refused: str = "",
    announce: Callable[[str], Awaitable[None]] | None = None,
    ending: Callable[[Callable[[], bool]], Awaitable[bool]] | None,
) -> bool:
    """Reflect, archive, clear in place. True when the surface actually did.

    ONE act for both answers. What the outcome decides is not what happens here
    but what happens afterwards: it is recorded on the checkpoint, and the
    reflection's completion callback delivers the continuity package for a
    CONTINUE and nothing for a RESET.

    The surface's ending is the only half that differs, and it is given the
    reflection to fire at its own point of no return. Reflection reads a
    snapshot taken synchronously when it is called, so it is unaffected by the
    clear that follows it.

    A `False` return means the boundary did not happen: the surface refused,
    or the clear did not reach disk. There is no fallback. The caller leaves
    the conversation standing, keeps the debt so the next turn retries, and
    says so at ERROR. Claiming otherwise would settle a debt the disk never
    heard about.
    """
    if ending is None:
        log.warning(
            "%s reached a boundary and this surface has no way to clear a "
            "conversation, so nothing was left behind",
            label,
        )
        return False

    def reflect() -> bool:
        return _reflect(
            app,
            session,
            chat_session,
            label=label,
            trigger=trigger,
            outcome=outcome.value,
            refused=refused,
            announce=announce,
        )

    try:
        ended = await ending(reflect)
    except Exception:
        log.exception("consolidation ending failed for %s", label)
        return False
    if not ended:
        # It said no, and it has already said why. Believing it anyway is how a
        # conversation that could not be written, or one the operator is not
        # even looking at, gets reported as consolidated.
        log.warning("%s could not be cleared, so it stands", label)
        return False
    log.info("consolidated %s (%s, %s)", label, trigger, outcome.value)
    # A CONTINUE says nothing here on purpose: the package IS what the person
    # is told, and it arrives when the reflection has something to put in it.
    # A second sentence now would be the app talking about itself twice.
    if outcome is Continuation.RESET and announce is not None:
        try:
            await announce(
                REFUSED_NOTICE.format(reason=refused) if refused else RESET_NOTICE
            )
        except Exception:
            log.exception("reset notice failed for %s", label)
    return True
