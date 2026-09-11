"""The recovery pass — a denial that cost something becomes something to decide.

A tripped breaker takes one capability off the board and everything else keeps
running, so from the outside it looks like a quiet day. Since the breaker heals
itself, most of what it turns away in the meantime is picked up by whatever
would have run again anyway: a sweep that fires every minute, a lint pass with a
schedule row, the next press of a card. Some of it is not. A spawn that finished
and had nowhere to go is gone, and nothing comes back for it.

That difference is the whole of this module, written as one list. It was a
judgement about each caller when it was written and it will be wrong about one
of them eventually, which is why it is a list in one place rather than the same
judgement made nine times at nine gates. `test_a_denial_that_cost_something.py`
fails when a gate appears that the list has never heard of.

Nothing here changes a cap, a config value or a line of code. It writes up the
one error and waits for an answer, because retrying is not a decision and
everything else is.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    ApprovalGate,
    mint_agenda_id,
)
from tesseract.orchestrator.workers.record import RiskClass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Caller:
    """One gate that asks a breaker for permission, and what a no costs there.

    `prefix` matches the start of the `subject` that gate passes to `allow`.
    `because` is written to be read by the operator, so it says what happened
    rather than which function it happened in.
    """

    prefix: str
    escalates: bool
    because: str


#: Every `CircuitBreaker.allow(subject=...)` in the runtime, and whether being
#: turned away there loses something. Order does not matter: no prefix here is a
#: prefix of another, and the test holds that true.
_CALLERS: tuple[_Caller, ...] = (
    _Caller(
        "spawn completion ",
        True,
        "a background task finished and its result had nowhere to go",
    ),
    _Caller(
        "undelivered completions at reconnect",
        True,
        "a chat came back owing a result and was not given it",
    ),
    _Caller(
        "card press in chat ",
        True,
        "you pressed something and nothing answered",
    ),
    _Caller(
        "wiki page for ",
        True,
        "nothing writes that page again on its own",
    ),
    _Caller(
        "embeddings for ",
        True,
        "nothing indexes that file again on its own, so it stays out of search",
    ),
    _Caller(
        "resuming chat ",
        True,
        "the app stopped in the middle of something in that chat and did not "
        "pick it back up, so what was left half done is still waiting",
    ),
    _Caller(
        "straggler completion in chat ",
        False,
        "the next turn on that chat looks again",
    ),
    _Caller(
        "overdue work in chat ",
        False,
        "the sweep that asked runs again within the minute",
    ),
    _Caller(
        "further card presses in chat ",
        False,
        "the next press asks for itself",
    ),
    _Caller(
        "contradiction check on ",
        False,
        "the job that asked runs the same pair again",
    ),
    # One row for the whole tool roster, and it stays one however many tools
    # there are: the gate names the CALL, and what is actually shut is the
    # dependency, which already reaches the operator as its own breaker. A row
    # per tool would be the enumeration `depends_on` exists to avoid.
    _Caller(
        "tool call ",
        False,
        "the assistant is told which dependency is down and can say so or try another way",
    ),
)


def caller_for(subject: str) -> _Caller | None:
    """Which gate a denial came from, or `None` for one nobody has judged yet."""
    for caller in _CALLERS:
        if subject.startswith(caller.prefix):
            return caller
    return None


def _slug(name: str, since: datetime | None) -> str:
    """An id fragment that is the same for every denial of one outage.

    One outage is one thing to decide about, so it gets one item that grows.
    Per-denial ids looked right until the vault indexer was considered: its gate
    is asked once per file, so a breaker open across a big ingest would have
    written a thousand items saying the same sentence about a thousand files.
    That is the wallpaper this phase's own exit criteria warn about.

    The trip time is what makes an outage an outage. A breaker that closes and
    trips again is a new one, and gets a new item.
    """
    stamp = since.isoformat() if since is not None else ""
    digest = hashlib.sha1(f"{name}\x00{stamp}".encode()).hexdigest()[:8]
    head = "".join(c if c.isalnum() or c == "-" else "-" for c in name.lower())
    head = "-".join(filter(None, head.split("-")))[:31]
    return f"{head}-{digest}"


def item_id_for(name: str, since: datetime | None, *, fallback: datetime | None = None) -> str:
    """The id this outage's item has, derivable by anyone who knows the trip.

    Both the writer and any later reader mint it from the same two facts, so
    asking "is someone on this?" needs no index and no extra field: the breaker
    log already records when the trip happened, which is all the name needs.
    """
    stamp = since or fallback or datetime.now(timezone.utc)
    return mint_agenda_id(_slug(name, since), now=stamp)


def owner_of(
    name: str, tripped_at: datetime | str | None, *, store: AgendaStore | None = None
) -> AgendaItem | None:
    """The item written for the outage this breaker is in, if there is one.

    `None` means nothing was ever written: either the breaker turned nothing
    away that mattered, or it has not turned anything away yet. An item that
    has reached a terminal status comes back all the same, because "you
    answered and it is still failing" is a different thing to say than "nobody
    has looked at this", and the caller has to be able to tell them apart.
    """
    since = tripped_at
    if isinstance(since, str):
        try:
            since = datetime.fromisoformat(since)
        except ValueError:
            return None
    if since is None:
        return None
    try:
        return (store or AgendaStore()).get(item_id_for(name, since))
    except Exception:  # noqa: BLE001 — a reader of this must never be the thing that breaks
        log.exception("recovery: could not look up who owns the %s outage", name)
        return None


def _when_text(since: datetime | None) -> str:
    if since is None:
        return "at an unrecorded time"
    return f"at {since.isoformat(timespec='seconds')}"


#: How many of the refused things the write-up names before it starts counting,
#: and how many characters those names may take. Both, because a vault subject
#: carries a file path: five of them can be longer than the whole write-up is
#: allowed to be, and the sentence that gets cut is the last one, which is the
#: one saying what to do about it.
_NAMED = 5
_NAMED_CHARS = 600


def _cost_text(refused: tuple[str, ...]) -> str:
    """What this outage has lost so far, named while the list is short."""
    losses = [s for s in refused if (c := caller_for(s)) is not None and c.escalates]
    if not losses:
        return ""
    reasons: list[str] = []
    for subject in losses:
        caller = caller_for(subject)
        if caller is not None and caller.because not in reasons:
            reasons.append(caller.because)
    named: list[str] = []
    spent = 0
    for subject in losses[:_NAMED]:
        if named and spent + len(subject) > _NAMED_CHARS:
            break
        named.append(subject)
        spent += len(subject) + 2
    rest = len(losses) - len(named)
    listed = ", ".join(named)
    if rest > 0:
        listed = f"{listed}, and {rest} more"
    count = "one thing" if len(losses) == 1 else f"{len(losses)} things"
    said = ". ".join(r[0].upper() + r[1:] for r in reasons)
    return f"It has turned away {count} that nothing comes back for: {listed}. {said}. "


def on_denial(
    name: str,
    subject: str,
    *,
    error: str = "",
    since: datetime | None = None,
    retry_in: float | None = None,
    refused: tuple[str, ...] = (),
    store: AgendaStore | None = None,
    now: datetime | None = None,
) -> AgendaItem | None:
    """Write up what this outage has cost, for the operator to answer.

    One item per outage, rewritten as the outage takes more, so the operator
    reads one growing thing rather than a page of near-identical ones.
    `refused` is everything the breaker has turned away so far, and this filters
    it to the part that matters.

    Returns the item, or `None` when the denial is one something else will
    retry, when the caller is one the list has never heard of, or when the write
    failed. Never raises: it is called from inside a permission gate, and a gate
    that fails because the paperwork failed is worse than no paperwork.
    """
    try:
        caller = caller_for(subject)
        if caller is None:
            # A gate the list has never seen. Silence is the safe half of the
            # two wrong answers, so this only says so; the test is what makes
            # it loud at the time the gate is written.
            log.warning(
                "recovery: %s turned away %r and no caller in recovery.py claims it, "
                "so nobody will hear about it",
                name, subject,
            )
            return None
        if not caller.escalates:
            return None
        return _write_item(
            name, subject,
            error=error, since=since, retry_in=retry_in,
            refused=refused or (subject,),
            store=store, now=now,
        )
    except Exception:  # noqa: BLE001 — a gate must not fail on this
        log.exception("recovery: escalating %s / %r failed", name, subject)
        return None


#: A refusal `BudgetExhausted` wrote: "<scope> budget exhausted for role=<role>:
#: spent ...". Anchored on ` spent` rather than the first colon, because a voice
#: role IS colon-separated (`voice:tts:gemini`) and a lazy match stopped inside
#: it, yielding `voice` and then looking up a scope nothing ever writes.
_BUDGET = re.compile(r"(\w+) budget exhausted for role=(.+?): spent")


def _cause_now(error: str, since: datetime | None) -> str:
    """Whether what stopped it is still true.

    One cause, because one cause is what the runtime can actually check without
    guessing: a spending cap. The rest of the write-up says what happened, and
    a sentence like this is what turns that into a decision, so it is worth
    having for the case that produced this whole plan.

    A cap is answered two ways. The operator may have approved going over,
    which is written down per day (`brain/cost/overage.py`) precisely so a
    reader outside the process that asked can see it. Or the day may simply
    have turned over, which gives every budget back.
    """
    found = _BUDGET.search(error or "")
    if found is None:
        return ""
    scope, role = found.group(1), found.group(2)
    # The same three cases `BudgetExhausted.scope_key` decides, and off the
    # same discriminant it uses: the scope word, not the shape of the role.
    if scope == "global":
        key = "global"
    elif scope == "voice":
        key = role
    else:
        key = f"role:{role}"

    today = date.today().isoformat()
    if since is not None and since.astimezone().date().isoformat() != today:
        return "That was a budget for an earlier day, and today's is fresh. "

    from tesseract.brain.cost import overage

    approved = overage.read(today, overage.unlocks_path()).get(key)
    if approved:
        return f"You approved going over that budget at {approved}, so the cap it stopped on is already lifted. "
    return ""


#: The error text is written down as the runtime recorded it, NOT through
#: `channel_turn._channel_safe_error`. That filter treats any slash as a path
#: marker, and the refusal this whole plan is about reads `spent $3.0617 / cap
#: $3.0000`, so filtering here replaces the one sentence the operator needs
#: with "I hit an error processing that" — measured, not assumed. Redaction
#: belongs at an outward boundary, where a runtime-composed sentence can be
#: sent instead of the raw text; this field feeds the local panel.
def _rationale(
    name: str,
    *,
    error: str,
    since: datetime | None,
    retry_in: float | None,
    refused: tuple[str, ...],
) -> str:
    from tesseract.context.circuit_breaker import retry_phrase

    said = f" It said: {error}." if error else ""
    return (
        f"The {name} breaker stopped trying {_when_text(since)}.{said} "
        f"{_cause_now(error, since)}"
        f"{_cost_text(refused)}"
        f"It is {retry_phrase(retry_in)} on its own. "
        "Nothing has been changed for you. If what stopped it is already fixed, "
        "breaker_reset clears it now. If it is not, the error above is the one "
        "to work on."
    )[:2000]


def _write_item(
    name: str,
    subject: str,
    *,
    error: str,
    since: datetime | None,
    retry_in: float | None,
    refused: tuple[str, ...],
    store: AgendaStore | None,
    now: datetime | None,
) -> AgendaItem | None:
    when = now or datetime.now(timezone.utc)
    store = store or AgendaStore()
    item_id = item_id_for(name, since, fallback=when)
    said = _rationale(
        name, error=error, since=since, retry_in=retry_in, refused=refused,
    )

    existing = store.get(item_id)
    if existing is not None:
        if existing.is_terminal():
            # The operator has answered about this outage. It taking more is
            # not a reason to put the same decision back in front of them.
            return existing
        existing.rationale = said
        existing.updated_at = when
        store.save(existing)
        return existing

    item = AgendaItem(
        id=item_id,
        created_at=when,
        updated_at=when,
        source=AgendaSource.RECOVERY,
        goal=f"{subject} was turned away and nothing comes back for it"[:500],
        rationale=said,
        risk_class=RiskClass.PROPOSE,
        approvals_required=[ApprovalGate(kind="operator_review", target=item_id)],
        status=AgendaStatus.AWAITING_OPERATOR,
    )
    try:
        store.add(item, by="recovery", reason="breaker_denial")
    except ValueError:
        # Another denial wrote this outage's item between the `get` and here.
        return store.get(item_id)
    log.info("recovery: %s refused %r, wrote %s", name, subject, item.id)
    return item


__all__ = ["caller_for", "item_id_for", "on_denial", "owner_of"]
