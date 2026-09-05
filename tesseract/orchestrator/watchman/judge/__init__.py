"""The four stages between reading the record and writing the report.

The watchman did two of six things and called the result a report: it
collected, then it rendered. Everything in between — the part that decides
whether a fact is still a fact — did not exist, and a live message proved what
that costs. The operator was told `brief_push: send_text failed` about a brief
that had been delivered three and a half hours earlier, by an error logged two
minutes after a restart the same report narrated in its own paragraph.

| # | Stage | The question |
| 1 | collect   | what did the sources write?              (`sources.py`) |
| 2 | resolve   | is it still true NOW?                    (`resolve.py`) |
| 3 | attribute | ours or theirs, inside a boot, an outage? (`attribute.py`) |
| 4 | suppress  | already said? resolved since?            (`suppress.py`) |
| 5 | rank      | needs the operator, or worth knowing?     (`rank.py`) |
| 6 | render    | prose                                    (`report.py`) |

**Every stage here is code.** The model appears once, at render, and only for
prose. A better narrator over an unjudged record writes a fluent paragraph
about a failure that was fixed hours ago, which is not a model failure and no
model fixes it.

**Nothing is deleted, and that is the point.** A stage does not remove a
finding, it records a VERDICT on it. What the operator is shown is the kept
set; what the Autonomy panel is shown is every verdict, so a person can see
what the judge held back and why. A judge nobody can inspect is a filter, and
a filter that silently drops a real fault is the failure this exists to
prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from tesseract.orchestrator.watchman.findings import Finding, SourceRead, Sweep

# In pipeline order. The panel renders them as a row each, so the order is
# the operator's reading order too.
STAGES = ("collect", "resolve", "attribute", "suppress", "rank")

KEPT = "kept"
DROPPED = "dropped"

# What stage 5 sorts into. Two tiers, because the operator's question is
# "must I do something" and everything else is context for when they look.
NEEDS_YOU = "needs_you"
WORTH_KNOWING = "worth_knowing"


@dataclass(frozen=True)
class Verdict:
    """One finding, and what the judge did with it.

    `why` is written for a person reading the Autonomy panel, not for a log.
    A verdict whose reason cannot be read is the same black box as no verdict.
    """

    finding: Finding
    stage: str
    action: str
    why: str
    tier: str = ""

    @property
    def kept(self) -> bool:
        return self.action != DROPPED


@dataclass(frozen=True)
class Judgement:
    """Every finding the sweep produced, and the judge's account of each."""

    sweep: Sweep
    verdicts: tuple[Verdict, ...]
    # Where a stage could not see the evidence its rule needs, in the reader's
    # own words. A rule that stops firing because the file it reads was pruned
    # goes quiet in exactly the way a working rule does, and the report then
    # carries findings the judge would have explained with no sign that
    # anything was missed. Saying so is cheaper than a second copy of the
    # evidence, and it is honest about which it is.
    blind_spots: tuple[str, ...] = ()

    @property
    def kept(self) -> tuple[Finding, ...]:
        return tuple(v.finding for v in self.verdicts if v.kept)

    @property
    def dropped(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if not v.kept)

    @property
    def needs_you(self) -> tuple[Finding, ...]:
        return tuple(v.finding for v in self.verdicts if v.kept and v.tier == NEEDS_YOU)

    @property
    def worth_knowing(self) -> tuple[Finding, ...]:
        return tuple(
            v.finding for v in self.verdicts if v.kept and v.tier == WORTH_KNOWING
        )

    def by_stage(self, stage: str) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.stage == stage)

    @property
    def judged(self) -> Sweep:
        """The sweep as the report should render it: kept findings only.

        A `Sweep` so that everything downstream — `fact_lines`, the summary
        body, the source table — keeps working on the shape it already knows,
        and so the per-source presence table still says which places were
        looked at even when everything they found was resolved.
        """
        keep = {id(f) for f in self.kept}
        # Stage 4 can ADD a finding — a recovery is news the sweep never saw —
        # so a kept finding with no source read of its own gets one. Without
        # this the recovery would be judged and then silently not rendered.
        reads = tuple(
            replace(read, findings=tuple(f for f in read.findings if id(f) in keep))
            for read in self.sweep.reads
        )
        known = {id(f) for read in reads for f in read.findings}
        added = tuple(f for f in self.kept if id(f) not in known)
        if added:
            reads = reads + (SourceRead(
                name="recovered", present=True, scanned=len(added), findings=added,
            ),)
        return replace(self.sweep, reads=reads)


def judge(sweep: Sweep, *, now: datetime) -> Judgement:
    """Run stages 2 to 5 over a sweep.

    Deliberately takes the whole `Sweep` rather than a list of findings: stage
    3 needs to know that a gap was common to every row before it can call it
    one outage, and that is a fact about the set, not about a finding.

    **Order is load bearing.** Suppress runs after the two filters, so a fault
    that resolve or attribute ruled out never enters the standing store and
    can never announce a recovery it did not have. Rank runs last because it
    is the only stage that does not decide survival, and it must not overwrite
    the reason an earlier stage gave.
    """
    from tesseract.orchestrator.watchman.judge import attribute, rank, resolve, suppress

    verdicts = [
        Verdict(finding=f, stage="collect", action=KEPT, why="read from the record")
        for f in sweep.findings
    ]
    verdicts = resolve.apply(verdicts, now=now)
    verdicts = attribute.apply(verdicts, now=now)
    verdicts = suppress.apply(verdicts, now=now)
    verdicts = rank.apply(verdicts, now=now)
    return Judgement(
        sweep=sweep,
        verdicts=tuple(verdicts),
        blind_spots=tuple(attribute.blind_spots(sweep, now=now)),
    )


__all__ = [
    "DROPPED",
    "KEPT",
    "NEEDS_YOU",
    "STAGES",
    "WORTH_KNOWING",
    "Judgement",
    "Verdict",
    "judge",
]
