"""Stage 4 — a standing failure is stated once, and so is its recovery.

The report becomes a report of TRANSITIONS. Three things can be true of a
fault on any tick, and only two of them are worth a message:

- **entered failing** — new since the last tick. Reported.
- **still failing** — reported an hour ago and still here. Silent, with how
  long it has been going carried on the finding for anyone who looks.
- **recovered** — reported before and gone now. Reported once, with what it
  was failing with and for how long, and then never again.

The nightly repetition of a known fault ends here. Not because repetition is
untidy, but because a channel that says the same thing every hour is one the
operator stops reading, and the hour it says something new is the hour they
miss it.

**Silence is not deletion.** A still-failing fault keeps a verdict saying so,
so the panel shows it and the summary's own held-back section names it. What
changes is whether it reaches the message.

**A repeat is silent because somebody is on it, not because it was mentioned.**
That premise was assumed rather than checked, and it was wrong for 26 hours: a
breaker tripped, was reported once, was silenced two minutes later as a repeat,
and nothing was working on it the whole time. The rule above is right for a
fault someone owns and wrong for one nobody has touched, and the difference is
knowable now: `orchestrator/autonomy/recovery.py` writes an agenda item for a
fault that cost something, and a breaker that is still probing is recovering by
itself. So a standing fault speaks a second time when nobody owns it and it
will not come back on its own. Once, because saying it every hour is the
problem this stage exists to solve.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from tesseract.orchestrator.watchman.findings import (
    INDEPENDENT_EVENT,
    UNDECLARED,
    Finding,
)
from tesseract.orchestrator.watchman.judge import standing

if TYPE_CHECKING:
    from tesseract.orchestrator.watchman.judge import Verdict

log = logging.getLogger(__name__)

STAGE = "suppress"

# The transitions, as the panel labels them.
ENTERED = "entered failing"
STILL = "still failing"
RECOVERED = "recovered"


def apply(verdicts: list["Verdict"], *, now: datetime) -> list["Verdict"]:
    """Mark repeats silent, and add one finding per recovery.

    Reads and rewrites the standing store, so it is the one stage with a side
    effect. That is deliberate and it is why it runs last among the filters:
    a fault dropped by resolve or attribute never enters the store, and so a
    fault that was start-up ordering does not later announce a recovery.
    """
    from tesseract.orchestrator.watchman.judge import DROPPED, KEPT, Verdict

    entries = standing.load()
    seen: set[str] = set()
    out: list[Verdict] = []

    for verdict in verdicts:
        if not verdict.kept:
            out.append(verdict)
            continue
        finding = verdict.finding
        if not finding.defect:
            # Only faults are tracked. A boot count or a governor pause is a
            # condition, and a condition repeating is what a condition does.
            out.append(verdict)
            continue

        key = standing.key_for(finding)
        seen.add(key)
        known = entries.get(key)
        if known is None or known.recovered_at is not None:
            entries[key] = standing.Standing(
                key=key, first_reported=now, last_seen=now, summary=finding.summary
            )
            out.append(verdict)
            continue

        standing_for = (
            f"{STILL}: first reported "
            f"{known.first_reported.isoformat(timespec='minutes')}, "
            f"{_for_how_long(known.first_reported, now)} and counting"
        )
        unowned = _nobody_is_on_it(finding)
        if not unowned:
            # Either somebody is on it, or this is a fault whose ownership
            # cannot be known. Silent, as it has always been.
            entries[key] = replace(
                known, last_seen=now, summary=finding.summary, announced_unowned=False,
            )
            out.append(Verdict(finding=finding, stage=STAGE, action=DROPPED, why=standing_for))
            continue
        if known.announced_unowned:
            entries[key] = replace(known, last_seen=now, summary=finding.summary)
            out.append(Verdict(
                finding=finding, stage=STAGE, action=DROPPED,
                why=f"{standing_for}, and that {unowned} has been said once already",
            ))
            continue
        entries[key] = replace(
            known, last_seen=now, summary=finding.summary, announced_unowned=True,
        )
        out.append(Verdict(
            finding=finding,
            stage=STAGE,
            action=KEPT,
            why=f"{standing_for}, and {unowned}",
        ))

    out.extend(_recoveries(entries, seen=seen, now=now))
    standing.save(entries, now=now)
    return out


def _nobody_is_on_it(finding) -> str:
    """Why this standing fault has earned a second mention, or "" for none.

    Only a breaker fault can answer this today, because a breaker is the only
    thing the runtime writes an owner for. Everything else returns "" and keeps
    the behaviour it has always had: mentioned once, then silent.

    Never raises. This stage decides what the operator hears, and a lookup
    failing is not a reason to either flood them or go quiet, so a failure
    leaves the fault exactly as suppression already had it.
    """
    if finding.source != "circuit-breakers" or finding.kind != "breaker_tripped":
        return ""
    try:
        from tesseract.context.circuit_breaker import is_recovering, row_for
        from tesseract.orchestrator.autonomy.recovery import owner_of
        from tesseract.paths import log_dir

        row = row_for(log_dir("circuit-breakers"), finding.subject)
        if row is None or not row["tripped"]:
            return ""
        # `opened_at`, not `tripped_at`: the write-up belongs to the OUTAGE,
        # and the trip time walks forward every time a probe fails.
        owner = owner_of(finding.subject, row.get("opened_at"))
        if owner is not None:
            if not owner.is_terminal():
                return ""
            return f"you answered {owner.id} and it is still failing"
        # Nothing was ever written for it, so nothing turned away by it was
        # worth writing up. That only leaves it worth saying again if it is not
        # coming back by itself, which is the one state a person has to end.
        if not is_recovering(row):
            return "nobody is working on it and it will not try again by itself"
        return ""
    except Exception:  # noqa: BLE001
        log.exception("watchman: could not tell whether %s is owned", finding.subject)
        return ""


def _recoveries(
    entries: dict[str, standing.Standing], *, seen: set[str], now: datetime
) -> list["Verdict"]:
    """One finding per fault that was here last tick and is gone now."""
    from tesseract.orchestrator.watchman.judge import KEPT, Verdict

    out: list[Verdict] = []
    for key, entry in list(entries.items()):
        if key in seen or entry.announced_recovery:
            continue
        if entry.recovered_at is None:
            entries[key] = replace(entry, recovered_at=now, announced_recovery=True)
            source, kind, subject = _split(key)
            out.append(Verdict(
                finding=Finding(
                    source=source,
                    kind="recovered",
                    # News that something cleared. There is nothing here to
                    # excuse, and a window that swallowed it would delete the
                    # one good line in the report.
                    by_boot=UNDECLARED,
                    by_outage=INDEPENDENT_EVENT,
                    subject=subject,
                    summary=(
                        f"{subject or source} has recovered. It was failing for "
                        f"{_for_how_long(entry.first_reported, now)}"
                    ),
                    model_summary=f"{kind} on {subject or source} has recovered",
                    first_at=entry.first_reported,
                    last_at=entry.last_seen,
                    evidence=(entry.summary,) if entry.summary else (),
                ),
                stage=STAGE,
                action=KEPT,
                why=f"{RECOVERED}: absent from this window, reported once and then not again",
            ))
    return out


def _split(key: str) -> tuple[str, str, str]:
    parts = key.split("/", 2)
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def _for_how_long(since: datetime, now: datetime) -> str:
    hours = max((now - since).total_seconds(), 0) / 3600
    if hours < 1:
        return f"{int(hours * 60)} minutes"
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


__all__ = ["ENTERED", "RECOVERED", "STAGE", "STILL", "apply"]
