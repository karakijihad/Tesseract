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
continue and reset: it asks, and a conversation that will not answer is cut
off rather than carried on. It used to CONTINUE by default, on the reading
that losing the room is recoverable and ending a conversation mid-work is not.
That reading only held while a reflection reconstructed the record afterwards;
continuing with no handoff now means continuing with nothing, which is not the
kinder answer, it is the one that loses the work quietly.

## One act, and the only difference is what follows it

Both answers do the same four things: reflect on a snapshot, archive what was
said, clear the conversation in place, keep the thread. Both then hand the
cleared conversation the package `brain/continuity.py` renders from the
checkpoint, because either way the next turn needs to know where the work
stood. What the answer decides is who reads it: a CONTINUE starts a turn
against the package at once, which is the whole of what the word means, and a
RESET leaves it in front of the conversation for whenever somebody speaks.
There is no second path and nothing branches on the trigger.

Clearing in place rather than opening a new conversation is the operator's
reading, 2026-09-06, and it is what the document asks for: a fresh CONTEXT,
which is an empty payload and the cost floor back at zero, not a fresh chat
record. The head does not move across the boundary, so the cached prefix
survives it.

## The record is the agent's, and it is here before the boundary is

The handoff comes in on `session_continue`, in the same call that asks for the
boundary, so by the time this runs the record already exists and the package
can be built the moment the conversation is cleared. It used to be built by the
reflection, which is a model turn over the whole transcript: the handover
waited for it (thirty-four seconds, measured), a reflection that failed cost
the work rather than the learning, and the guard against that made a hung
provider call into a conversation that could never consolidate. None of that is
bounded here; it is gone.

A HARD boundary is the one case with no call in flight to carry a handoff. The
runtime asks for one, on the conversation itself, and tells the person the
ceiling was reached and what happens if no answer comes. Two asks and no more:
a conversation that will not say where the work stood is cut off, cleared, and
carries nothing.

There is no fallback. A consolidation that could not clear leaves the
conversation STANDING, with the boundary still owed so the next turn retries,
and says so at ERROR. There used to be a fold here, a second mechanism
answering the same question with a different act, and on its worst branch it
summarised the middle away immediately after the runtime had failed to archive
a copy of it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from tesseract.brain.chat import Continuation, Handoff
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


#: What the CONVERSATION is asked when it hit the ceiling without saying where
#: the work stood. Addressed to the model and read by the person over its
#: shoulder, so it says the consequence before the request.
ASK_FOR_THE_HANDOFF = (
    "This conversation has reached the size it can work at, so it is being "
    "wrapped up now whatever happens next. Say where the work stands, with "
    "`session_continue`: what this was for, what got done, what is left, and "
    "what to do first. Choose `continue` if the work goes on and `reset` if it "
    "is finished. Without that call the conversation is cleared and nothing is "
    "carried over."
)


#: What a person is told when a conversation was asked and would not answer.
#: It names the act rather than softening it: the runtime broke a conversation
#: off, and a notice that read like an ordinary reset would hide the only thing
#: about this worth knowing.
CUT_OFF_NOTICE = (
    "Cut off. I asked this conversation twice where the work stood and got no "
    "answer, so it is being cleared and nothing is carried over. What it "
    "taught me is still being written down. Tell me what you want done next "
    "and I will start from there."
)


#: Why the record says a cut-off conversation was stopped. One sentence,
#: because it is written onto the checkpoint and read by a person.
CUT_OFF_REASON = (
    "it was asked twice where the work stood and never said, so there was "
    "nothing to carry"
)


#: How many times the runtime asks a conversation for a handoff it did not
#: volunteer. Two, and then it is cut off.
#:
#: The circuit breaker every retry loop here has, and NOT the kind of tally
#: that fires on how often something happened without evidence that the thing
#: it counts is what is wrong. The difference is what the count is evidence
#: of: a limit on how many times a conversation may carry on counted
#: boundaries and concluded something about the CONTEXT, which it had no
#: evidence for, and it was deleted for stopping real multi-phase work. This
#: counts asks that went unanswered and concludes that asking again will go
#: unanswered too, which is the thing itself.
MAX_HANDOFF_ASKS = 2


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
    carry_on: Callable[[str, str], Awaitable[None]] | None = None,
    ending: Callable[[Callable[[], None]], Awaitable[bool]] | None = None,
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

    `carry_on` starts a turn on this conversation whose body is the continuity
    package, and is called only when the answer was CONTINUE. It is the third
    surface callback beside `announce` and `ending`, because starting a turn
    is the third thing only the surface knows how to do: the cockpit reaches a
    chat by its id over a WebSocket, a channel reaches one by its Telegram id.
    A caller that omits it leaves the package in front of the conversation and
    the work waits for somebody to speak, which is what both surfaces did
    before this existed.

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

    asked = _requested(chat_session, label)
    soft = asked is not None
    nudge = _take_nudge(chat_session, label)
    if not soft and not _boundary_forced(chat_session, label):
        # Carrying on. Not a mode, not a decision the runtime made: nobody
        # asked for a boundary and the window has room, so there is nothing to
        # do. There is something to record only if the observer had asked for
        # one: a recommendation read and not acted on is the evidence
        # that says whether these are worth making.
        _journal_nudge(nudge, answered=None, label=label)
        return

    trigger = "soft" if soft else "hard"
    handoff = asked
    refused = ""

    if handoff is None:
        # A hard boundary with nothing in flight to carry a handoff. Ask the
        # conversation for one, on itself, and leave the boundary owed so the
        # end of that turn takes it. Nothing is cleared here: the record has
        # to be written BEFORE the conversation it describes is emptied.
        if await _ask_for_a_handoff(app, chat_session, label, announce, carry_on):
            _journal_nudge(nudge, answered=None, label=label)
            return
        # Asked, and asked again, and it never said. The conversation is cut
        # off rather than carried on, which is what "no handoff, no continue"
        # means when there is nobody left to ask. There is no record, so there
        # is no package either, and that falls out rather than being enforced.
        log.warning("%s is being cut off: %s", label, CUT_OFF_REASON)
        refused = CUT_OFF_REASON
        answer = Continuation.RESET
    else:
        answer = handoff.mode
        if answer is Continuation.CONTINUE:
            refused = _why_not_continue(handoff, label)
            if refused:
                # It reported nothing left to do, which is an answer and not
                # an omission: the work is finished, so the conversation is
                # left behind rather than carried on, and the record says why.
                # A later reader most needs to tell that apart from a
                # conversation the runtime stopped.
                log.info("%s may not carry on: %s", label, refused)
                answer = Continuation.RESET

    if await _consolidate(
        app,
        session,
        chat_session,
        label=label,
        trigger=trigger,
        outcome=answer,
        handoff=handoff,
        refused=refused,
        announce=announce,
        carry_on=carry_on,
        ending=ending,
    ):
        _journal_nudge(nudge, answered=answer, label=label)
        _record_boundary(
            session,
            label,
            trigger=trigger,
            outcome=answer.value,
            refused=refused,
        )
        _forget_boundary_owed(chat_session, label)
        return

    # Everything below is the boundary that did NOT happen, and the two lines
    # first are about not lying in the record for it.
    #
    # `answered=None`: the nudge was read and no boundary was taken. Journalled
    # before the consolidation, a refused clear wrote the observer's
    # recommendation down as FOLLOWED, on a turn where nothing happened. The
    # row exists so the operator can see which recommendations are worth
    # making, and a miss counted as a hit is the one reading it must not give.
    _journal_nudge(nudge, answered=None, label=label)
    # And the turn's own decision goes back where it was found, the handoff
    # with it. `_requested` reads by CLEARING, so a refused clear consumed
    # both the `continue` the agent asked for and the record of where the work
    # stood, and the next turn met the plain threshold knowing nothing about
    # either. The debt below is kept for exactly this reason; the decision is
    # half of the same debt.
    if asked is not None:
        put_back = getattr(chat_session, "hand_back_continuation", None)
        if put_back is not None:
            try:
                put_back(asked)
            except Exception:
                log.exception("could not hand %s back its own decision", label)

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


def _requested(chat_session: Any, label: str) -> Handoff | None:
    """What the turn asked for and where it said the work stood, or `None`.
    Reading it clears it.

    One object because it was one call. A session that cannot answer is not an
    error: sub-agent sessions and test doubles have no continuation to give,
    and they are the ordinary case for everything that is not a conversation
    with a person in it.
    """
    take = getattr(chat_session, "take_continuation", None)
    if take is None:
        return None
    try:
        asked = take()
    except Exception:
        log.exception("reading the turn's decision failed for %s", label)
        return None
    if not isinstance(asked, Handoff):
        if asked:
            # `request_continuation` builds these, so anything else means
            # something wrote the field directly. Say so, and fall through to
            # the threshold rather than guessing what was meant.
            log.error("turn asked for an unreadable continuation %r on %s", asked, label)
        return None
    return asked


def _why_not_continue(handoff: Handoff, label: str) -> str:
    """Why this conversation may not carry the work on, or `""`.

    Asked of the handoff the agent just wrote, which is the only place the
    answer is: whether there is anything left to do is the agent's own report,
    and there is exactly one refusal, which is that it reported none.

    It used to read the boundaries this conversation had already crossed,
    because the record was written afterwards by a reflection and this
    boundary's did not exist yet. `continuity` says why the two rules that
    lived there are deleted rather than moved.
    """
    try:
        from tesseract.brain import continuity
        from tesseract.orchestrator import checkpoints

        return continuity.why_not_continue(
            checkpoints.build(
                session_id="", trigger="", outcome="", state=handoff.state
            )
        )
    except Exception:
        log.exception("reading what %s said was left failed", label)
        return ""


async def _ask_for_a_handoff(
    app: Any,
    chat_session: Any,
    label: str,
    announce: Callable[[str], Awaitable[None]] | None,
    carry_on: Callable[[str, str], Awaitable[None]] | None,
) -> bool:
    """Ask this conversation where the work stood. True when it was asked.

    Only a HARD boundary reaches here. A soft one arrives carrying its handoff,
    because the call that asks for the boundary is the call that writes it; a
    hard one is the runtime noticing, with nothing in flight to answer into.

    `False` means the asking is over: either it has been asked as often as it
    is going to be, or this surface has no way to start a turn at all. The
    caller cuts the conversation off, and that is the whole of the loop's
    bound.

    The turn is SPAWNED and not awaited. This runs at the end of a turn, inside
    the caller's own flow, and awaiting a turn from there would nest one inside
    another; the same reasoning `carry_on` already rests on. The end of THAT
    turn reaches this function again, finds the boundary still owed, and by
    then either has a handoff or does not.

    Never raises. The turn it is called at the end of has already landed.
    """
    if carry_on is None:
        log.warning(
            "%s reached a hard boundary with no handoff and this surface "
            "cannot ask for one, so the conversation is cut off",
            label,
        )
        return False
    count = _count_the_ask(chat_session, label)
    if count is None or count > MAX_HANDOFF_ASKS:
        return False
    if announce is not None:
        try:
            await announce(ASK_FOR_THE_HANDOFF)
        except Exception:
            log.exception("could not tell %s that it had reached the ceiling", label)
    log.info("asking %s where the work stood (ask %d of %d)", label, count, MAX_HANDOFF_ASKS)
    try:
        from tesseract.mirror.server.ws_connection import _spawn_tracked

        _spawn_tracked(
            app, carry_on(ASK_FOR_THE_HANDOFF, "handoff_asked"), f"handoff_ask:{label}"
        )
    except Exception:
        log.exception("could not ask %s where the work stood", label)
        return False
    return True


def _count_the_ask(chat_session: Any, label: str) -> int | None:
    """How many times this conversation has now been asked, or `None` when it
    cannot be asked at all.

    `None` for a session that cannot hold the count, which is a sub-agent or a
    test double. Asking one of those would be an unbounded loop with nothing
    recording that it had gone round, so it is treated as already spent.
    """
    count_it = getattr(chat_session, "asked_for_a_handoff", None)
    if count_it is None:
        return None
    try:
        return int(count_it())
    except Exception:
        log.exception("could not count the handoff asked of %s", label)
        return None


def _reflect(app: Any, session: Any, chat_session: Any, *, label: str) -> None:
    """Distil what this conversation taught, and nothing else.

    It used to answer whether the conversation could be cleared, and the one
    thing that answer ever meant was "a previous reflection is still running".
    That mattered only while reflection wrote the record a boundary hands
    over; it writes none now, so a reflection that is busy, short or broken
    costs the learning from one conversation and never its continuity.

    Never raises, and nothing waits on it. The boundary has been decided.
    """
    if app is None:
        # No inbox to file a proposal in. A missing surface, not a boundary
        # that may not happen.
        log.warning("reflection skipped for %s: no app to reach the inbox with", label)
        return
    try:
        from tesseract.mirror.server.reflect import start_reflection

        start_reflection(app, session, chat_session, reason="turn_decision", label=label)
    except Exception:
        # `start_reflection` catches its own, so what is left here is the
        # import. It is caught anyway because this runs INSIDE the surface's
        # ending, at its point of no return, and `_consolidate` reads an
        # ending that raised as a boundary that did not happen. A reflection
        # costing the work is the exact shape this phase exists to remove.
        log.exception("reflection could not be started for %s", label)


async def _consolidate(
    app: Any,
    session: Any,
    chat_session: Any,
    *,
    label: str,
    trigger: str,
    outcome: "Continuation",
    handoff: "Handoff | None",
    refused: str = "",
    announce: Callable[[str], Awaitable[None]] | None = None,
    carry_on: Callable[[str, str], Awaitable[None]] | None = None,
    ending: Callable[[Callable[[], None]], Awaitable[bool]] | None,
) -> bool:
    """Record, reflect, archive, clear in place, hand over. True when the
    surface actually cleared.

    ONE act for both answers. What the outcome decides is not what happens here
    but what happens afterwards: the package is handed over either way, and a
    CONTINUE also starts a turn against it.

    **The record is built before the clear and written after it.** Built
    before, because it describes the conversation that is about to be emptied
    and reads the active project off disk; written after, because a boundary
    that could not clear did not happen, and a row claiming it did would be
    read back by the next one as a boundary this conversation crossed.

    The surface's ending is the only half that differs, and it is given the
    reflection to fire at its own point of no return. Reflection reads a
    snapshot taken synchronously when it is called, so it is unaffected by the
    clear that follows it, and it answers nothing: a boundary no longer waits
    on what it learns.

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

    record = _build_the_record(
        session,
        chat_session,
        label=label,
        trigger=trigger,
        outcome=outcome,
        refused=refused,
        handoff=handoff,
    )

    def reflect() -> None:
        _reflect(app, session, chat_session, label=label)

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
    if not _write_the_record(record, label):
        record = None
    await _hand_over(
        app,
        session,
        chat_session,
        label=label,
        outcome=outcome,
        refused=refused,
        record=record,
        announce=announce,
        carry_on=carry_on,
    )
    return True


def _build_the_record(
    session: Any,
    chat_session: Any,
    *,
    label: str,
    trigger: str,
    outcome: "Continuation",
    refused: str,
    handoff: "Handoff | None",
) -> Any:
    """This boundary's checkpoint, or `None` when there is nothing to record.

    `handoff is None` is the cut-off conversation: it was asked where the work
    stood and never said, so there is nothing to write down and nothing to
    invent. An empty row would be worse than none, because the absence of a
    checkpoint is how a later reader knows a boundary handed nothing over.

    Never raises. The boundary is decided and the conversation is about to be
    cleared; nothing here may turn that into a failed turn.
    """
    if handoff is None:
        return None
    try:
        from tesseract.orchestrator import checkpoints

        context = getattr(chat_session, "tool_context", None)
        return checkpoints.build(
            session_id=str(getattr(session, "session_id", "") or ""),
            chat_id=str(getattr(context, "chat_id", "") or ""),
            trigger=trigger,
            outcome=outcome.value,
            refused=refused,
            state=handoff.state,
        )
    except Exception:
        log.exception("the record for %s could not be built", label)
        return None


def _write_the_record(record: Any, label: str) -> bool:
    """Put this boundary's record on disk. True when it landed.

    The store's own answer, not the object handed to it: `write` returns the id
    it appended, or `None` when the disk would not take it, and a record that
    never landed must not be handed on as one that did. `False` means the
    package is built from nothing, which is honest, rather than from the
    PREVIOUS boundary's row, which is what reading the store back would give.

    Never raises. The conversation has already been cleared.
    """
    if record is None:
        return False
    try:
        from tesseract.orchestrator import checkpoints

        if checkpoints.write(record) is None:
            log.warning("the record for %s was not written", label)
            return False
        return True
    except Exception:
        log.exception("the record for %s was not written", label)
        return False


async def _hand_over(
    app: Any,
    session: Any,
    chat_session: Any,
    *,
    label: str,
    outcome: "Continuation",
    refused: str,
    record: Any,
    announce: Callable[[str], Awaitable[None]] | None,
    carry_on: Callable[[str, str], Awaitable[None]] | None,
) -> bool:
    """Give the cleared conversation what the boundary wrote down, and tell
    the person.

    Built HERE, moments after the clear, from the record the agent wrote at the
    start of all this. It used to ride the reflection's completion callback,
    because the record did not exist until that model turn finished, and the
    whole apparatus that went with it goes too: the package no longer lists
    what reflection saved, and nothing has to ask whether the conversation it
    was written for is still the one on screen, because no detached call runs
    in between.

    A RESET leaves the package in front of the conversation for whoever speaks
    next. A CONTINUE starts a turn against it, and the person is told first:
    they see where the work stood before the work moves on it.

    Never raises. The boundary has happened.
    """
    from tesseract.brain import continuity

    text = ""
    try:
        text = continuity.package_for(record)
    except Exception:
        log.exception("the package for %s could not be built", label)
    if outcome is Continuation.RESET and announce is not None:
        try:
            await announce(
                CUT_OFF_NOTICE
                if refused == CUT_OFF_REASON
                else REFUSED_NOTICE.format(reason=refused) if refused
                else RESET_NOTICE
            )
        except Exception:
            log.exception("reset notice failed for %s", label)
    if not text:
        return False
    if getattr(session, "torn_down", False):
        # The surface dropped this session while the clear was in flight.
        # Writing into its history and persisting it puts a package into a
        # conversation nobody can reach, and starting a turn on it streams to
        # a socket nobody holds. CC-16's hole, in the shape this path has it.
        log.info("continuity: %s ended before the package could land", label)
        return False
    carrying = outcome is Continuation.CONTINUE and carry_on is not None
    if not carrying:
        # The half a RESET leaves behind, and what a CONTINUE falls back to on
        # a surface that cannot start a turn of its own. The carried half does
        # not call this: that turn's own first message carries the same text
        # under the same kind of mark, and noting it here as well would put the
        # package into the history twice.
        if not _note_the_package(chat_session, label, text):
            return False
        await asyncio.to_thread(_persist, session, label)
    if announce is not None:
        try:
            await announce(text)
        except Exception:
            log.exception("continuity: could not tell %s about it", label)
    # Last, so the person has the package in front of them before the work
    # moves on it.
    if carrying:
        _carry_the_work_on(app, carry_on, text, label)
    return True


def _note_the_package(chat_session: Any, label: str, text: str) -> bool:
    """Put the package in front of the cleared conversation, to be read
    whenever somebody next speaks."""
    try:
        chat_session.note_continuity(text)
    except Exception:
        log.exception("continuity: could not put the package in front of %s", label)
        return False
    return True


def _persist(session: Any, label: str) -> None:
    """Write the package to disk with the conversation it was put in front of.

    The boundary persisted an EMPTY history on its way past, because at that
    moment the package did not exist. Left to the periodic autosave, a reload
    inside its interval shows a thread the boundary cleared and nothing saying
    why. It is one write and it lands with the record already open.
    """
    try:
        from tesseract.mirror.server import chat_store

        chat_store.persist_session_chats(session)
    except Exception:
        log.exception("continuity: the package was not written to disk for %s", label)


def _carry_the_work_on(app: Any, carry_on: Any, text: str, label: str) -> None:
    """Start the turn that reads the package, and return without awaiting it.

    `continue` says the work goes on, and until CC-26 nothing started it: the
    package landed in a cleared conversation and waited for somebody to speak.
    Measured on the operator's phone, 2026-09-10, at a boundary whose record
    carried a filled `Remaining` and `Next`: nothing moved until they typed.

    **Spawned here rather than awaited, and that is structural rather than a
    convention the surfaces have to remember.** This runs at the end of a turn,
    inside that turn's own flow, and a turn awaited here would be one turn
    nested inside another. Spawning is also what makes the loop a loop rather
    than a stack that deepens once per boundary.

    Never raises. The boundary has already happened and the package has already
    been handed over; a turn that could not be started is one thing going
    wrong, not two.
    """
    try:
        from tesseract.mirror.server.ws_connection import _spawn_tracked

        _spawn_tracked(app, carry_on(text, "carry_on"), f"carry_on:{label}")
    except Exception:
        log.exception("continuity: %s could not carry the work on", label)
