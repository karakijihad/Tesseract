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

Continuing currently means folding, because the continuity tail that would
rebuild the room from the checkpoint is not built yet. That is the one place
this module is a step short of the design rather than an expression of it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from tesseract.brain.chat import Continuation
from tesseract.memory.log_notes import append_log_entry
from tesseract.paths import log_dir

log = logging.getLogger(__name__)

#: What a person is told when the conversation folds. Plain words: they need
#: to know the older detail is summarised now, and that asking for it still
#: works, which is the only thing that changes for them. It lives beside the
#: decision that fires it so there is one sentence and not one per surface.
COMPACTED_NOTICE = (
    "This conversation got long, so I summarised the earlier part to keep "
    "going. Recent turns are intact. Ask me about anything older and I will "
    "read it back from the record."
)

#: What a person is told when the turn decided it was finished with this
#: conversation. Same reason `COMPACTED_NOTICE` lives here: one sentence,
#: beside the moment that fires it, rather than one per surface.
RESET_NOTICE = (
    "That work is done, so I am starting fresh here to keep this quick. I am "
    "writing down what it taught me. Ask me about any of it and I will read it "
    "back from the record."
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


def _record_compaction(session: Any, label: str, before: int, after: int) -> None:
    """The tally and the log line. Both surfaces, always. Never raises.

    `compact_count` feeds the `[session_end]` entry, and the `[auto_compact]`
    entry is the runtime's own record that a fold happened at all. A channel
    was missing from both, so a compaction on a phone left no trace anywhere a
    later session could read.

    The channel's tally is write-only for now: `[session_end]` is written by
    `ws_connection._autosave` on WebSocket teardown, which a channel session
    never reaches. The `[auto_compact]` entry is what actually carries a
    channel compaction into the log, and that one both surfaces share.
    """
    try:
        session.compact_count += 1
    except Exception:
        log.exception("compact_count increment failed for %s", label)
    session_id = str(getattr(session, "session_id", "") or "")
    try:
        now = datetime.now(timezone.utc)
        ratio_pct = round((1 - after / before) * 100, 1) if before else 0.0
        append_log_entry(
            header=f"## [auto_compact] Compaction {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            body=(
                f"Auto-compact fired (session={session_id[:8]}, "
                f"turn={getattr(session, 'turn_count', 0)}).\n"
                f"Tokens before: {before}  |  Tokens after: {after}  |  "
                f"Ratio: {ratio_pct}%"
            ),
            log_dir=log_dir("sessions"),
            date=now,
        )
    except Exception:
        log.exception("logs/sessions [auto_compact] append failed for %s", label)


async def after_turn(
    chat_session: Any,
    *,
    app: Any,
    session: Any,
    channel: str | None = None,
    chat_id: str | None = None,
    announce: Callable[[str], Awaitable[None]] | None = None,
    report: Callable[[int, int], Awaitable[None]] | None = None,
    ending: Callable[[Callable[[], bool]], Awaitable[bool]] | None = None,
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
    it now compacts here and nowhere else.

    `announce` sends one line of text to the person. `report` hands the two
    token counts to a surface that can draw them. A caller passes whichever
    its transport has, or neither.

    A fold that changed nothing is not a compaction, and nothing downstream
    hears about it. `compact()` returns `(before, before)` both when it had
    nothing to fold and when the summarizer came back empty; counting those,
    logging them as `[auto_compact]`, or delivering them tells every reader
    that history was summarised when it was not touched. The cockpit drew
    `Auto-compacted 4000 -> 4000 tok` for exactly that, because the rule was
    written into the `announce` gate and not into the moment itself.

    `app` is where the workspace event store lives, which a reflection has to
    reach. Required, and not something a caller can get wrong: both surfaces
    have exactly one and the cockpit's hook was already being handed it.

    `ending` is what this surface does with a conversation the turn has
    finished with. The cockpit archives it and opens a new chat; a channel has
    no chat to switch to and wipes in place. Only the ENDING differs, and a
    caller that has none simply cannot be asked to reset. It returns whether
    it actually ended the conversation, because a cockpit that could not write
    the transcript, or whose turn ran in a chat the operator is not looking at,
    refuses rather than archiving the wrong thing.

    It is HANDED the reflection rather than following it, because the moment
    reflection may fire is the moment the conversation is certainly being left,
    and only the surface knows where that is: for the cockpit it is after the
    transcript is written, and a reflection fired ahead of a persist that then
    failed would have paid for a model turn and filed a proposal about a
    conversation still going on. For a channel the wipe cannot fail, so it is
    immediate.

    The turn's own decision outranks the threshold. `session_continue` records
    one and this is where it is honoured, because `compact()` and `reset()`
    rewrite history in place and doing that mid-turn discards the assistant
    message carrying the pending `tool_use` block. A turn that decided nothing
    meets exactly the threshold check it met before this existed.

    Never raises. The turn has already landed and been sent, and nothing here
    is allowed to turn a delivered answer into a failed one.
    """
    label = _label(session, channel, chat_id)
    answer = _requested(chat_session, label)
    soft = answer is not None
    if not soft and not _threshold_reached(chat_session, label):
        # Carrying on. Not a mode, not a decision the runtime made: nobody
        # asked for a boundary and the window has room, so there is nothing to
        # do and nothing to record.
        return

    if answer is None:
        # A hard boundary the turn did not answer for, which is every hard
        # boundary until CC-7 lets the reflection call return the answer with
        # its deltas. Fold, never reset: losing the room is recoverable and
        # ending a conversation the operator was in the middle of is not.
        answer = Continuation.CONTINUE

    if answer == Continuation.RESET and await _reset(
        app,
        session,
        chat_session,
        label=label,
        trigger="soft" if soft else "hard",
        announce=announce,
        ending=ending,
    ):
        return

    # Everything that reaches here continues: the turn asked to, the threshold
    # forced it, or a reset the surface could not perform fell back to one.
    # Reflection first and on a snapshot, because the fold below rewrites the
    # history it reads. `reflect_in_background` refuses to stack, so a reset
    # whose ending already reflected does not pay twice on the way through.
    _reflect(
        app,
        session,
        chat_session,
        label=label,
        trigger="soft" if soft else "hard",
        outcome=Continuation.CONTINUE.value,
    )

    try:
        # `should_compact()` is the rule and `compact()` is the act. Asking the
        # rule again here would be a second answer to the question that decided
        # this boundary was due.
        result = await chat_session.compact()
    except Exception:
        log.exception("fold failed for %s", label)
        return
    if result is None:
        return
    before, after = result
    if after == before:
        # Worth a line: a threshold that keeps being crossed while the
        # summarizer keeps coming back empty is a real fault, and silence here
        # is what would hide it.
        log.info("compaction folded nothing for %s (%d tokens)", label, before)
        return
    log.info("compacted %s: %d to %d tokens", label, before, after)

    _record_compaction(session, label, before, after)

    if report is not None:
        try:
            await report(before, after)
        except Exception:
            log.exception("compaction report failed for %s", label)
    if announce is not None:
        try:
            await announce(COMPACTED_NOTICE)
        except Exception:
            log.exception("compaction notice failed for %s", label)


def _threshold_reached(chat_session: Any, label: str) -> bool:
    """Whether the window is full enough that a boundary is mandatory.

    Asked before reflection so the snapshot the reflection reads is the
    conversation about to be folded, rather than what is left of it. That
    ordering is why this is the only place the threshold is read: a helper that
    checked it again on its way to folding would be answering a question this
    boundary has already answered.

    A session that cannot answer has no threshold to cross. Sub-agent sessions
    and test doubles are the ordinary case for that, exactly as in `_requested`.
    """
    should = getattr(chat_session, "should_compact", None)
    if should is None:
        return False
    try:
        return bool(should())
    except Exception:
        log.exception("reading the compaction threshold failed for %s", label)
        return False


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


def _reflect(
    app: Any,
    session: Any,
    chat_session: Any,
    *,
    label: str,
    trigger: str = "",
    outcome: str = "",
) -> bool:
    """Distil what this conversation taught, without changing its shape."""
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
        )
    except Exception:
        log.exception("reflection failed for %s", label)
        return False


async def _reset(
    app: Any,
    session: Any,
    chat_session: Any,
    *,
    label: str,
    trigger: str,
    announce: Callable[[str], Awaitable[None]] | None,
    ending: Callable[[Callable[[], bool]], Awaitable[bool]] | None,
) -> bool:
    """Leave this conversation behind. True when the surface actually did.

    The surface's ending is the only half that differs, and it is given the
    reflection to fire at its own point of no return. Reflection reads a
    snapshot of the conversation being left, taken synchronously, so it is
    unaffected by the wipe or the chat swap that follows.

    A `False` return means the handoff did not happen and the caller falls
    through to the ordinary threshold check. Claiming otherwise would leave a
    conversation growing under a turn that believes it started fresh.
    """
    if ending is None:
        log.warning(
            "%s asked to reset and this surface has no way to end a "
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
            outcome=Continuation.RESET.value,
        )

    try:
        ended = await ending(reflect)
    except Exception:
        log.exception("reset ending failed for %s", label)
        return False
    if not ended:
        # It said no, and it has already said why. Believing it anyway is how a
        # conversation that could not be written, or one the operator is not
        # even looking at, gets reported as reset.
        log.warning("%s could not be left behind, so it stands", label)
        return False
    log.info("left %s behind", label)
    if announce is not None:
        try:
            await announce(RESET_NOTICE)
        except Exception:
            log.exception("reset notice failed for %s", label)
    return True
