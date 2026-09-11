"""One row per fault kind: what answers it, and what is recorded about it.

A standing fault is silent after its first mention, and that rule was right for
the thing it was written against: a channel that says the same sentence every
hour is one the operator stops reading. It is wrong for a fault the runtime has
something to SAY about. "The breaker tripped" said once and then never is a
fault going quiet; "the breaker tripped, the cooldown probe has been retrying
for three days and it is still failing" is a fault the operator can end.

So a fault whose kind is in this table earns another mention while it stands,
once a day, naming what answers it and whether that has run. A kind not in the
table keeps the behaviour it has: said once.

**A row that nothing records says so.** `Remedy.read` is `None` where nothing
on this machine writes down that the remedy ran, and the sentence then says
that rather than reporting a zero. The same shape as `spent.MEASURES`, and for
the same reason: a number nobody produces is not the number nought.

**The table grows with its remedies, never ahead of them.** A row here is a
promise that the thing it names exists, so a kind whose answer has not been
built has no row. `governor.py`'s pause is the one thing that answers a
question here without a row: a pause is the runtime working rather than a
fault, so it produces no finding and reaches `stop_rule.advice_owed` directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)


#: The three reasons a remedy may not run unasked. A closed set, declared here
#: because the row is what carries one, and re-exported from `stop_rule` where
#: they are the whole of the *needs advice* condition. Anything outside these
#: three is not a reason to stop, and a remedy that needs a fourth is a
#: decision the operator has not been asked for yet.
SPENDS = "it would spend money"
SEALED_TREE = "it would write inside the sealed app tree"
UNSAFE = "it is on the unsafe list"
ASK_REASONS: tuple[str, ...] = (SPENDS, SEALED_TREE, UNSAFE)


@dataclass(frozen=True)
class Attempts:
    """What the record says about one remedy running against one subject.

    `last is None` and `failures == 0` from a reader that RAN means the remedy
    has not been attempted, which is a different fact from `Remedy.read` being
    `None`, which means nothing would have recorded it either way. The sentence
    keeps the two apart because the operator's next move differs: one is a
    remedy that has not fired, the other is a remedy nobody can audit.
    """

    last: datetime | None = None
    failures: int = 0
    given_up: bool = False


@dataclass(frozen=True)
class Remedy:
    """What the runtime does about one kind of fault.

    `asks` is empty for a remedy the runtime may run unasked, and otherwise
    carries one of `stop_rule.ASK_REASONS`. It is the whole of the *needs
    advice* condition: the reason a remedy has to ask is declared here, on the
    remedy, and never worked out from how bad the fault looks.
    """

    #: The `Finding.kind` this answers. One row per kind, and the table is
    #: keyed on it.
    kind: str
    #: What the remedy is called, in the words the operator reads.
    title: str
    #: What it does, one plain sentence. It goes into the message.
    what_it_does: str
    #: "" when the runtime may run it unasked. Otherwise why it may not, in one
    #: of the three declared reasons.
    asks: str = ""
    #: Reads what is recorded about this remedy for one subject. `None` when
    #: nothing on this machine records it, which the sentence then says.
    read: Callable[[str], Attempts] | None = None


def _breaker_probe(subject: str) -> Attempts:
    """What the breaker's own row says about its cooldown probe.

    `tripped_at` walks forward every time a probe fails, so it is when the
    remedy last ran rather than when the outage began. `opened_at` is the
    outage, and `suppress.py` uses that one for exactly the opposite reason.
    """
    from tesseract.context.circuit_breaker import is_recovering, row_for
    from tesseract.paths import log_dir

    row = row_for(log_dir("circuit-breakers"), subject)
    if row is None or not row.get("tripped"):
        return Attempts()
    return Attempts(
        last=_parse(row.get("tripped_at")),
        failures=int(row.get("trips_since_reset") or 0),
        given_up=not is_recovering(row),
    )


def _repair_attempts(subject: str) -> Attempts:
    """What `repairs.py`'s attempt log says about one declared repair.

    The subject IS the repair's key: `sources.read_repairs` puts it there for
    exactly this reason, so a reader matches the same thing tick to tick
    rather than parsing it back out of a sentence.

    Walked from the beginning rather than from the tail, because the count that
    matters is consecutive failures SINCE the last success, and a window over
    the end of the file would report a repair that has been failing for a week
    and one that failed twice this morning as the same number.
    """
    from tesseract.orchestrator.repairs import attempts_path

    path = attempts_path()
    if not path.exists():
        return Attempts()
    last: datetime | None = None
    failures = 0
    given_up = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("key") != subject:
            continue
        at = _parse(row.get("at"))
        outcome = str(row.get("outcome") or "")
        if at is not None:
            last = at
        if outcome == "repaired":
            failures = 0
            given_up = False
        elif outcome == "failed":
            failures += 1
        elif outcome == "held":
            given_up = True
    return Attempts(last=last, failures=failures, given_up=given_up)


#: The closed table. Adding a kind means adding it here, and a kind that is not
#: here behaves exactly as it did before this module existed.
REMEDIES: tuple[Remedy, ...] = (
    Remedy(
        kind="breaker_tripped",
        title="the cooldown probe",
        what_it_does=(
            "the breaker waits, tries the call again on its own, and closes "
            "itself the first time one works"
        ),
        read=_breaker_probe,
    ),
    Remedy(
        kind="pass_incomplete",
        title="running the stage by hand",
        what_it_does=(
            "one step of the daily upkeep is run now, which settles the day "
            "into memory and moves the watermark the check reads"
        ),
        # A stage can call a provider, which is the line healing does not
        # cross on its own. `pipeline_run_stage` is `ask` for the same reason.
        asks=SPENDS,
    ),
    Remedy(
        kind="repair_held",
        title="the runtime's own repair",
        what_it_does=(
            "the runtime tries this on every sweep and stops after three "
            "failures in a row, which is where it is now"
        ),
        # No `asks`: the repair runs unasked and has already given up, which
        # is the *kernel bugged* condition rather than a decision withheld.
        # The finding's own summary carries the `breaker_reset` that starts it
        # again once the cause is gone.
        read=_repair_attempts,
    ),
    Remedy(
        kind="provider_drift",
        title="the next model in the role's list",
        what_it_does=(
            "every role that leads with this model falls through to the next "
            "one it names, so the work carries on while this one is refusing"
        ),
        # Continuous rather than an event: a chain tries the next entry on the
        # call itself, and nothing writes down that it did.
        read=None,
    ),
)

_BY_KIND: dict[str, Remedy] = {remedy.kind: remedy for remedy in REMEDIES}


def remedy_for(kind: str) -> Remedy | None:
    """The declared answer to this kind of fault, or `None` for a kind the
    table has never heard of."""
    return _BY_KIND.get(kind)


def attempts_for(remedy: Remedy, subject: str) -> Attempts | None:
    """What is recorded about this remedy, or `None` when nothing is.

    Never raises. This decides what the operator hears, and a reader failing is
    not a reason to either invent an attempt or go quiet, so a failure reads as
    nothing recorded.
    """
    if remedy.read is None:
        return None
    try:
        return remedy.read(subject)
    except Exception:  # noqa: BLE001
        log.exception("healing: could not read what %s has tried", remedy.kind)
        return None


def sentence_for(remedy: Remedy, subject: str, *, now: datetime) -> str:
    """What the operator is told about the remedy, in one sentence.

    Written for a message, so it names the remedy first: the fault is already
    in the line above it and what the reader does not have is what is being
    done about it.
    """
    said = f"{remedy.title} answers this: {remedy.what_it_does}"
    if remedy.asks:
        return f"{said}. It is not run without asking, because {remedy.asks}"
    attempts = attempts_for(remedy, subject)
    if attempts is None:
        return f"{said}. Nothing on this machine records whether it has run"
    if attempts.given_up:
        return f"{said}. It has stopped trying{_after(attempts)}"
    if attempts.last is None:
        return f"{said}. It has not run yet"
    return f"{said}. It last tried {_ago(attempts.last, now)}{_after(attempts)}"


def _after(attempts: Attempts) -> str:
    if attempts.failures < 1:
        return ""
    times = "once" if attempts.failures == 1 else f"{attempts.failures} times"
    return f", having failed {times}"


def _ago(when: datetime, now: datetime) -> str:
    """How long ago, rounded down, in the same words the pipeline reader uses.

    Rounded down because a report may not overstate itself: an hour and fifty
    minutes is one hour, and the operator acts on the number.
    """
    minutes = max((now - when).total_seconds(), 0) / 60
    if minutes < 60:
        return f"{int(minutes)} minutes ago"
    if minutes < 60 * 48:
        return f"{int(minutes // 60)} hours ago"
    return f"{int(minutes // (60 * 24))} days ago"


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


__all__ = [
    "ASK_REASONS",
    "REMEDIES",
    "SEALED_TREE",
    "SPENDS",
    "UNSAFE",
    "Attempts",
    "Remedy",
    "attempts_for",
    "remedy_for",
    "sentence_for",
]
