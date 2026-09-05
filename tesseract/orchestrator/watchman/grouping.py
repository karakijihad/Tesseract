"""Four lines that are one thing, rendered as one thing.

The report repeats itself, and no producer is at fault. Four sleep windows are
four findings because each one is a real window. Four backend errors are four
findings because each one is a real logger. Nothing anywhere records that the
four windows are one machine that kept going to sleep, or that three of the
loggers died on the same exception, so the report cannot say it either.

This is the reader saying it. Two keys, and both are already on the finding:

- **the same condition, recurring** is the same source, kind and subject. The
  sleep windows share all three, and every one of them is `schedule` /
  `runtime_stalled` / no subject.
- **the same cause** is the sentence at the bottom of the traceback. Four
  loggers have four subjects and one cause, which is why `subject` alone
  cannot group them and why `cause` is on the finding at all.

**A grouping is a rendering, not an edit.** Nothing here writes, nothing here
drops, and the judge never sees it: its stages ask questions per subject and
keep state across ticks, and a merged subject would answer them about
something that has no history. The verdicts the Autonomy panel draws stay one
per finding. What changes is only what a person is shown in one pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from tesseract.lib.log_envelope import SEVERITIES
from tesseract.orchestrator.watchman.findings import Finding

# Grouping by cause stays inside one source. A cause crossing sources would be
# a bigger claim than the evidence supports: two subsystems logging the same
# `ConnectionError: [Errno 111]` are usually two faults with one flavour, and
# only the backend's collector reads a traceback far enough to know better.
_CAUSE_KEY = "cause"
_CONDITION_KEY = "condition"


def _key(finding: Finding) -> tuple[str, ...]:
    if finding.cause:
        return (_CAUSE_KEY, finding.source, finding.kind, finding.cause)
    return (_CONDITION_KEY, finding.source, finding.kind, finding.subject)


def group(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    """One finding per incident, in the order the first member arrived.

    Order is the input's, not the group's, so a report does not reshuffle
    itself between passes because a second member of an old group showed up.
    """
    buckets: dict[tuple[str, ...], list[Finding]] = {}
    for finding in findings:
        buckets.setdefault(_key(finding), []).append(finding)
    return tuple(_merge(members) for members in buckets.values())


def _merge(members: list[Finding]) -> Finding:
    if len(members) == 1:
        return members[0]
    newest = max(members, key=_recency)
    others = len(members) - 1
    # The newest member speaks for the group and the rest are listed under it.
    # A composed sentence would need to know what these findings MEAN to say
    # anything better than "four of them", and that knowledge is exactly the
    # per-source special-casing this reader is shrinking.
    if newest.cause:
        tail = (
            f", and {others} more error that died on the same cause"
            if others == 1
            else f", and {others} more errors that died on the same cause"
        )
        model_tail = f", and {others} more from the same cause"
    else:
        tail = model_tail = f", and {others} more like it in this window"
    return replace(
        newest,
        subject=newest.cause or newest.subject,
        summary=newest.summary + tail,
        model_summary=(newest.summary_for_model + model_tail),
        count=sum(f.count for f in members),
        first_at=min((f.first_at for f in members if f.first_at), default=None),
        last_at=max((f.last_at for f in members if f.last_at), default=None),
        # The cause first, then ONE line accounting for the whole group. A
        # line per member reads well for three and not at all for thirty, and
        # the report prints two evidence lines per finding, so the members
        # would be silently cut off at the second. The raw log lines the
        # members carried are dropped here and kept where they belong: the
        # evidence file, which is written per finding and never grouped,
        # because a report handed upstream is about one failure.
        evidence=((newest.cause,) if newest.cause else ())
        + (_roll_up(members, newest),),
        # The worst member speaks for the incident, and a group is only safe
        # to forward if every member was.
        severity=max((f.severity for f in members), key=SEVERITIES.index),
        quotable=all(f.quotable for f in members),
    )


def _roll_up(members: list[Finding], newest: Finding) -> str:
    """One line saying what else is in the group, in the runtime's own words.

    Two facts, both of which the reader already holds: how far the group
    reaches, and where else it was reported. The second is only worth saying
    when the members came from different places, which is the cause groups and
    never the recurring ones, and it names only the places the headline does
    not already carry.
    """
    span = [f.last_at or f.first_at for f in members]
    known = sorted(t for t in span if t is not None)
    when = (
        f"between {known[0].isoformat(timespec='seconds')} and "
        f"{known[-1].isoformat(timespec='seconds')}"
        if len(known) > 1 and known[0] != known[-1] else "in this window"
    )
    line = f"{len(members)} of these, {when}"
    elsewhere = sorted(
        {f.subject for f in members if f.subject and f.subject != newest.subject}
    )
    if elsewhere:
        line += f", also: {', '.join(elsewhere)}"
    return line


def _recency(finding: Finding):  # noqa: ANN202 — a sort key, never read
    return (
        finding.last_at is not None,
        finding.last_at,
        finding.first_at is not None,
        finding.first_at,
    )


__all__ = ["group"]
