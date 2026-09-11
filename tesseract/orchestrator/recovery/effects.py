"""What was half-done when the machine stopped, and who decides about it.

Every other scan in this package asks what STATE was left behind: a worker
record with no terminal transition, a turn manifest still in `open/`, an agenda
item pointing at a worker that is gone. This one asks the question none of them
can: **was an effect on the outside world left in the air.**

The four scans beside it can close what they find, because a record is ours to
close. An effect is not. A message either reached somebody or it did not, and
the only thing that decides what recovery may do about it is what the tool
declared before it ran (`kernel/tools/recovery.py`):

  ``read_only``   never recorded, so never seen here. Repeating it is free.
  ``idempotent``  the same call again is the same effect. Resume, and say so.
  ``queryable``   something can be asked what happened. Check before repeating.
  ``unsafe``      nobody can say whether it landed. The operator decides.

**The declaration is read off the ROW, never off the registry.** The row was
written when the call was made; a tool can be edited, and a custom one can be
retired, between then and this boot. Asking the registry again would be
answering about today's tool rather than the one that ran.

**Nothing here retries anything.** The unsafe case is a question, and the
question is the whole point: the operator's own words for it were "except for
cases when she wants my consult/advice". The other two are recorded on the
summary so the conversation that picks the work up knows what it is standing
in, which is the half a resumed turn needs and the half a card cannot carry.

**One card per uncertain call, and it is asked once.** The id is derived from
the call, so a second boot over the same wreckage finds its own question
already standing and adds nothing. A card the operator already answered is
never re-asked: a fault carrying on is not a reason to ask again, which is the
rule `healing/stop_rule.py` settled.
"""

from __future__ import annotations

import logging
from datetime import datetime

from tesseract.lib.clock import parse_stamp, to_local
from tesseract.orchestrator.checkpoints import Checkpoint
from tesseract.workspace_events.events import EventStore, WorkspaceEvent

log = logging.getLogger(__name__)

#: What a call's declaration means for this pass, in the operator's language.
#: `read_only` is absent deliberately: `brain/tools.py::execute_tool` writes no
#: boundary for one, so a row carrying it would be a bug rather than a state.
_WHAT_IT_MEANS: dict[str, str] = {
    "idempotent": (
        "running it again is the same act, not a second one, so the work can "
        "carry on from here"
    ),
    "queryable": (
        "it can be checked before anything is repeated, and the check comes "
        "first"
    ),
    "unsafe": (
        "nothing here can say whether it happened, so it waits for you"
    ),
}

#: The bucket a behaviour is counted under on the recovery summary.
BUCKET: dict[str, str] = {
    "idempotent": "resumable",
    "queryable": "check_first",
    "unsafe": "asked_you",
}


def card_id_for(call_id: str) -> str:
    """One question per call, for as long as the question stands."""
    return f"effect-{call_id}"


def when_of(row: Checkpoint) -> str:
    """When the call was made, on the clock the person reading this is on.

    A checkpoint's stamp is UTC, which is the rule for an instant. A time in a
    sentence somebody reads is a wall clock, which is the other half of the
    same rule, and this was slicing the stored string: a card on a phone said
    07:00 about a call made at 09:00. Both readers of the sentence go through
    here so there is one answer to what time it was.
    """
    moment = parse_stamp(row.ts)
    return to_local(moment).strftime("%Y-%m-%d %H:%M") if moment else "an unknown time"


def describe(row: Checkpoint, *, name_the_place: bool = True) -> str:
    """What was in the air, in a sentence a person reads on a phone.

    `name_the_place` is off for the one reader that is already standing in it.
    A card is read in an inbox, where "in chat 9f2c" is the only thing saying
    which conversation this was about; the brief `recovery/resume.py` hands a
    resumed turn is delivered INTO that conversation, where the same clause is
    an opaque id the turn has no use for. One sentence, two readers, and the
    only difference between them is whether the place still needs saying.
    """
    where = row.chat_id or row.run_id or "a turn with no conversation behind it"
    place = f" in {where}" if name_the_place else ""
    when = when_of(row)
    parked = " It had stopped to ask you when the machine went down." if (
        row.boundary == "awaiting_operator"
    ) else ""
    return (
        f"{row.tool} was called at {when}{place} and the machine stopped "
        f"before it could record what happened.{parked} "
        f"By its own declaration, {_WHAT_IT_MEANS.get(row.recovery, _WHAT_IT_MEANS['unsafe'])}."
    )


def build_card(row: Checkpoint) -> WorkspaceEvent:
    """The one question an uncertain effect puts to the operator.

    A `clarification`, which is the kind that already means "the runtime is
    asking and a person answers in the thread": it reaches every surface,
    `/queue` on the phone counts it, and the answer comes back as a comment
    the assistant reads. A new kind would have been a second answer to a
    question this store settled, and a kind no surface can draw is a card
    nobody sees.

    **It asks and does not offer a button that runs anything.** Approving a
    `clarification` flips a status and nothing more: `apply_decision` has no
    branch for the kind, and the cockpit's Inbox leaves it out of
    `ACTIONABLE_KINDS` and draws one Resolve verb. So the card says what is
    uncertain, says plainly that nothing has been repeated, and asks for the
    answer in words. Copy promising a retry would have been describing a
    remedy this runtime does not have yet, which is the failure the house
    copy rule names first.

    Written at `pending` with no decision on it, because what happens next is
    the operator's and no default here is the runtime's to take.
    """
    return WorkspaceEvent(
        event_id=card_id_for(row.call_id),
        ts=datetime.now().astimezone().isoformat(),
        kind="clarification",
        source="recovery",
        title=f"Did {row.tool} finish before the machine stopped?",
        summary=(
            f"{describe(row)} Nothing has been repeated and nothing will be "
            "until you say so. Tell me here whether to run it again, check "
            "first, or leave it, and I will do that in your next turn."
        ),
        payload={
            "call_id": row.call_id,
            "tool": row.tool,
            "recovery": row.recovery,
            "chat_id": row.chat_id,
            "run_id": row.run_id,
            "boundary": row.boundary,
            "checkpoint_id": row.checkpoint_id,
            "at": row.ts,
        },
        priority=8,
        author_id="system",
        author_display="Recovery",
    )


#: One card per boot for calls that ran with no record of them, rather than one
#: per call. The store keys by id and the newest row wins, so re-filing rewrites
#: the card with the newest call rather than adding to a pile: the operator
#: needs to know the window happened and roughly when, not to answer the same
#: question forty times. It carries the boot so a window that closed and a
#: window that is still open are different cards.
def unrecorded_card_id(boot: str) -> str:
    return f"unrecorded-{boot}"


def build_unrecorded_card(
    *, tool: str, call_id: str, behaviour: str, boot: str
) -> WorkspaceEvent:
    """The card for a call that went ahead while nothing could write it down.

    `execute_tool` refuses an `unsafe` call when its record will not write,
    because nothing can check that class afterwards. The other classes go
    ahead, which is the rule this machine runs on: a disk that will not take a
    note must not turn the runtime off. But `queryable` means "ask the far side
    before deciding", and the only thing that ever ASKS is a brief built from
    the row that just failed to write. So the guarantee was checkable in
    principle and prompted to nobody.

    This is the prompt. It is the same `clarification` kind an uncertain effect
    already uses, so it reaches the phone by the path that already exists
    rather than a second one.
    """
    return WorkspaceEvent(
        event_id=unrecorded_card_id(boot),
        ts=datetime.now().astimezone().isoformat(),
        kind="clarification",
        source="recovery",
        title="Something ran that I could not write down",
        summary=(
            f"{tool} ran while the checkpoint store would not take a note, so "
            "if this machine stops now there will be nothing saying the call "
            "was made. Anything done in this window may need checking by hand. "
            "The disk or the permissions on the home tree are the place to "
            "look."
        ),
        payload={
            "tool": tool,
            "call_id": call_id,
            "recovery": behaviour,
            "boot": boot,
        },
        priority=8,
        author_id="system",
        author_display="Recovery",
    )


def file_unrecorded(
    store: EventStore, *, tool: str, call_id: str, behaviour: str, boot: str
) -> None:
    """Say that a call ran unrecorded. Never raises: a card never fails a turn."""
    try:
        store.append_event(
            build_unrecorded_card(
                tool=tool, call_id=call_id, behaviour=behaviour, boot=boot
            )
        )
    except Exception:  # noqa: BLE001 - the turn is not the card's to fail
        log.exception("recovery: could not say that %s ran unrecorded", tool)


def file_question(store: EventStore, row: Checkpoint) -> bool:
    """Put one uncertain effect in front of the operator. True when it was new.

    Asked once and only once. The id is the call's, so this reads the store
    first and leaves any existing card exactly as it is: a pending one is the
    same question already waiting, and a settled one is a question that was
    answered. Re-appending either would be the runtime asking a person to
    decide something twice, which is how an inbox stops being read.
    """
    card_id = card_id_for(row.call_id)
    if store.get_event(card_id) is not None:
        return False
    store.append_event(build_card(row))
    return True
