"""Where the runtime stops healing and asks, declared in one list.

The rule the operator gave: it keeps going, whatever breaks, until the kernel
is bugged or their advice is owed. Those are two conditions and they are here,
in code, because the alternative is deciding case by case how bad a log line
looks and that decision is never the same twice.

**The kernel is bugged.** The declared remedy has run and given up, and the
fault it answers is still standing. A repair gives up when its breaker opens
after three failures; a breaker's cooldown probe gives up when there is no
retry coming. Both mean the same thing: the thing that was supposed to fix this
has been tried to its own limit and the fault has not moved.

**Your advice is owed.** The remedy is one the runtime may not run on its own,
for one of three declared reasons: it would spend, it would write inside the
sealed app tree, or it is on the unsafe list. The reason lives on the remedy
row, so this module reads a declaration rather than making a judgement.

The same condition has a second door, `advice_owed`, for a caller that holds
the evidence itself. The governor is the only one: a source it has lifted and
that came straight back is a decision about that source, and no finding
carries it, because a pause is the runtime working rather than a fault. Both
doors mint the same card, which is the reason the second one is here and not
at the caller.

Everything not on the list heals and says so. A fault with no declared remedy
reaches the operator through the report exactly as it always has, and nothing
here files a card about it: a card for every unanswered fault is the wallpaper
the whole plan warns about.

**The card is the recovery pass's card.** One shape for "something happened
that only you can answer", so the panel, the inbox and the phone all already
know how to show it, and one per standing episode rather than one per tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from tesseract.orchestrator.healing.remedies import (
    ASK_REASONS,
    SEALED_TREE,
    SPENDS,
    UNSAFE,
    Remedy,
    attempts_for,
    remedy_for,
)

log = logging.getLogger(__name__)

#: The two conditions, in the words the card uses. Nothing else stops the
#: runtime, and a third would be a change to what the operator agreed to.
KERNEL_BUGGED = "the kernel is bugged"
NEEDS_ADVICE = "your advice is owed"
CONDITIONS: tuple[str, ...] = (KERNEL_BUGGED, NEEDS_ADVICE)


@dataclass(frozen=True)
class Stop:
    """One fault the runtime will not carry on its own, and why."""

    condition: str
    kind: str
    subject: str
    #: The declared row this stop came off, or `None` where the caller knows
    #: the recurrence and no finding carries it. A governor pause is the one
    #: such case: a pause is a condition rather than a fault, so it never
    #: enters the standing store and no remedy row can be read for it.
    remedy: Remedy | None
    #: What the operator is being asked, written for them to read.
    said: str


def assess(*, kind: str, subject: str) -> Stop | None:
    """Whether this standing fault is one the runtime stops on.

    `None` for everything the runtime keeps going through, which is the
    overwhelmingly common answer: a fault with no declared remedy, and a fault
    whose remedy is still trying.

    Never raises. It is called from inside a sweep that has already read the
    record, and a paperwork failure must not cost the report.
    """
    try:
        remedy = remedy_for(kind)
        if remedy is None:
            return None
        if remedy.asks:
            return Stop(
                condition=NEEDS_ADVICE,
                kind=kind,
                subject=subject,
                remedy=remedy,
                said=(
                    f"{subject or kind} is still failing. What would fix it is "
                    f"{remedy.title}: {remedy.what_it_does}. The runtime has not "
                    f"done it, because {remedy.asks}"
                ),
            )
        attempts = attempts_for(remedy, subject)
        if attempts is None or not attempts.given_up:
            return None
        tried = ""
        if attempts.failures > 0:
            times = "once" if attempts.failures == 1 else f"{attempts.failures} times"
            tried = f" after failing {times}"
        return Stop(
            condition=KERNEL_BUGGED,
            kind=kind,
            subject=subject,
            remedy=remedy,
            said=(
                f"{subject or kind} is still failing and {remedy.title} has "
                f"stopped trying{tried}. Everything the runtime can do about "
                f"this on its own has been done, so what is left is a fault in "
                f"the code rather than a state it can recover from"
            ),
        )
    except Exception:  # noqa: BLE001
        log.exception("healing: could not assess %s on %r", kind, subject)
        return None


def advice_owed(*, kind: str, subject: str, said: str) -> Stop:
    """The *needs advice* condition, declared by a caller that holds the
    evidence itself.

    The same condition and the same card as the one `assess` reaches off a
    remedy row; what differs is where the knowledge lives. The governor knows
    that a source it has lifted twice came straight back, and nothing else
    does: a pause is the runtime working rather than a fault, so it produces no
    finding, enters no standing store, and has no row for `assess` to read.

    Here rather than at the caller so that both ways of reaching this
    condition mint the same shape, and so the list of what stops the runtime
    stays readable in one file.
    """
    return Stop(
        condition=NEEDS_ADVICE, kind=kind, subject=subject, remedy=None, said=said
    )


#: What separates the two halves of a fault identity before it is hashed.
#: A character neither a kind nor a subject can contain, so ("a", "b.c")
#: and ("a.b", "c") cannot hash to one card.
SEPARATOR = "\x00"


def card_id(stop: Stop, *, since: datetime | None) -> str:
    """The id this stop's card has, derivable by anyone holding the same fault.

    Minted from the fault's identity and when it started, so a fault standing
    for a week updates one card and a fault that clears and returns gets a new
    one. The same reasoning as `recovery.item_id_for`, and for the same reason:
    the reader needs to find the card without an index.

    **The full identity is hashed in, and the readable part is only a label.**
    `mint_agenda_id` truncates a slug to 40 characters, so a readable form
    alone collides on any two subjects that agree in their opening: a breaker's
    subject is its registered name, and those are model refs of the shape
    `<tier>.<provider>.<model_id>`, so two sibling models under one provider
    share far more than 40 characters. Two of them tripping in the same minute
    minted ONE id, and `file_card` would have quietly rewritten the first
    card's rationale instead of filing the second. The digest is
    `recovery._slug`'s answer to the same problem.
    """
    import hashlib

    from tesseract.orchestrator.autonomy.models import mint_agenda_id

    stamp = since or datetime.now(timezone.utc)
    identity = SEPARATOR.join((stop.kind, stop.subject))
    digest = hashlib.sha1(identity.encode()).hexdigest()[:8]
    head = f"{stop.kind}-{stop.subject}"[:30]
    return mint_agenda_id(f"{head}-{digest}", now=stamp)


def file_card(stop: Stop, *, since: datetime | None = None, store=None, now=None):
    """Put the stop in front of the operator, once per standing episode.

    Returns the item, or `None` when the write failed. An item the operator has
    already answered comes back unchanged: the fault carrying on is not a
    reason to ask the same question again.

    Never raises, for the reason `recovery.on_denial` does not: this runs
    inside a sweep whose report is worth more than its paperwork.
    """
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import (
        AgendaItem,
        AgendaSource,
        AgendaStatus,
        ApprovalGate,
    )
    from tesseract.orchestrator.workers.record import RiskClass

    try:
        when = now or datetime.now(timezone.utc)
        store = store or AgendaStore()
        item_id = card_id(stop, since=since)
        existing = store.get(item_id)
        if existing is not None:
            if existing.is_terminal():
                return existing
            existing.rationale = _rationale(stop)
            existing.updated_at = when
            store.save(existing)
            return existing
        item = AgendaItem(
            id=item_id,
            created_at=when,
            updated_at=when,
            source=AgendaSource.RECOVERY,
            goal=f"{stop.subject or stop.kind}: {stop.condition}"[:500],
            rationale=_rationale(stop),
            risk_class=RiskClass.PROPOSE,
            approvals_required=[ApprovalGate(kind="operator_review", target=item_id)],
            status=AgendaStatus.AWAITING_OPERATOR,
        )
        try:
            # `recovery`, because this IS the recovery pass's card and
            # `TransitionActor` is a closed list. A fourth actor would be a new
            # word for the same thing on every row that reads one.
            store.add(item, by="recovery", reason=stop.condition)
        except ValueError:
            # Another pass wrote this episode's card between the read and here.
            return store.get(item_id)
        log.info("healing: %s on %r, wrote %s", stop.condition, stop.subject, item.id)
        return item
    except Exception:  # noqa: BLE001
        log.exception("healing: could not file the card for %r", stop.subject)
        return None


def _rationale(stop: Stop) -> str:
    return (
        f"{stop.said}. Nothing has been changed for you. "
        f"Answering this card is what decides what happens next."
    )[:2000]


__all__ = [
    "ASK_REASONS",
    "CONDITIONS",
    "KERNEL_BUGGED",
    "NEEDS_ADVICE",
    "SEALED_TREE",
    "SPENDS",
    "UNSAFE",
    "Stop",
    "advice_owed",
    "assess",
    "card_id",
    "file_card",
]
