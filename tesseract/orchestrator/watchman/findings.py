"""What the watchman found, and what it could not look at.

Two shapes, and the second one is the point. A finding is something that
happened; a source read is the account of a place that was looked at — whether
it existed, how much of it was in the window, and what came out. A collector
that returns an empty list because the directory is missing and one that
returns an empty list because the runtime was healthy are the same value and
opposite facts, and a summary built from findings alone cannot tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tesseract.lib.log_envelope import BAD, INFO, SEVERITIES, WARN

# Evidence lines are quoted verbatim into a report the operator may hand
# upstream, so they are capped per finding rather than by total size: ten lines
# of one failure says what it is, and a thousand says nothing at all.
MAX_EVIDENCE_LINES = 10
MAX_EVIDENCE_CHARS = 400

# Whether a window may account for this finding, and where it may not, why.
#
# The judge's third stage drops a fault that began and ended while the machine
# was away, or while the runtime was still coming up. Which findings it may do
# that to used to be two hardcoded sets of kind strings inside that stage, and
# that could not work for two reasons measured rather than supposed:
#
#   - The list is CLOSED and the vocabulary is OPEN. `read_supervisor` passes
#     an incident's own `event` string through as its kind, so the set of
#     kinds is not enumerable anywhere. A list keyed on it cannot be complete
#     even in principle, and any kind added later is silently not explained.
#   - The reasons a finding is not explainable are not one reason. Three
#     different ones were already in the stage's own comment, and a single
#     list flattened them into "absent", so nothing could say WHY.
#
# So each producer declares it, because the producer is the only thing that
# knows what its own vocabulary means. Every value but `EXPLAINABLE` behaves
# identically, leaving the finding in the report; they differ in what the
# verdict can say about it.
EXPLAINABLE = "explainable"
# True NOW rather than something that happened in a window, so no window can
# account for it however long the machine was away. A provider that is still
# refusing and an interpreter that is still the wrong one are both this.
CURRENT_STATE = "current state"
# Another stage already applied the same window, and applying it twice would
# excuse the finding on evidence that has been spent.
ALREADY_ACCOUNTED = "already accounted"
# It happened anyway and is its own evidence. A breaker that tripped recorded
# a real trip whether or not the machine went away around it.
INDEPENDENT_EVENT = "independent event"
# This finding IS the explaining circumstance. A machine absence cannot be
# excused by a machine absence.
IS_THE_EXPLANATION = "is the explanation"
# Nobody said. Behaves as not explainable, which is the safe direction: an
# unexplained fault still reaches the operator, while a wrongly explained one
# is gone. `test_every_shipped_finding_declares_itself` is what stops this
# being the quiet default forever.
UNDECLARED = "undeclared"


@dataclass(frozen=True)
class Finding:
    """One counted thing that happened, with the lines that prove it."""

    source: str
    kind: str
    summary: str
    # WHAT this finding is about, in a form something other than a person can
    # match on: a row name, a ref, a breaker, a logger, a worker kind.
    #
    # The judge is why this exists. Every stage after collection has to ask a
    # question about the SAME thing — has this row fired since, did this
    # provider answer on its next probe, was this fault already reported last
    # tick — and until now the only identifier a finding carried was a
    # sentence with the name embedded in it. A judge that has to parse
    # `the consolidate row ended 2 of 2 run(s)` to learn the word
    # `consolidate` is a judge built on a rendering.
    #
    # Empty means the finding is about the runtime as a whole (an outage, a
    # boot count), which is a real answer and not a missing one.
    subject: str = ""
    # WHY, where the reader can see it: the sentence that ends the traceback,
    # the exception a batch of unrelated-looking errors all died on.
    #
    # `subject` answers "is this the same thing as last hour", which is what
    # the judge needs. It does not answer "are these four records one
    # problem", which is what the report needs, and the two questions have
    # different answers: four errors from four loggers have four subjects and
    # one cause. Empty means the reader could not see one, which is most
    # findings and is not a failure.
    #
    # It is never shown to the model. An exception message is text the runtime
    # was handed, and `model_summary` exists for exactly that reason.
    cause: str = ""
    count: int = 1
    first_at: datetime | None = None
    last_at: datetime | None = None
    evidence: tuple[str, ...] = ()
    # How bad this is, in the same three words every record on disk now uses.
    #
    # It was a `defect` boolean, and a boolean cannot tell the operator apart
    # from a fault that broke something and one that made something slower.
    # Both got the same treatment, so the report had to keep a hand-written
    # list of kinds that were "technically defects but not really" to stop a
    # restart count reading like an outage. Three words remove the list:
    #
    #   bad   something is broken and stayed broken. Red.
    #   warn  something is degraded, late, or looping. Orange.
    #   info  the runtime describing itself. Green, and never sent.
    #
    # The COLLECTOR decides, because it is the only thing that knows what its
    # own vocabulary means, and for the five enveloped streams it reads the
    # severity the writer already declared rather than deciding again.
    severity: str = INFO
    # True when this finding's evidence may be sent to the MODEL, which for
    # most roles means off this machine.
    #
    # The evidence always reaches the operator's own files. What this gates is
    # the narration's input, and the two are not the same risk.
    #
    # This shipped with provider-health and schedule marked quotable, on the
    # reasoning that a provider's `reason` and a stage's `outcome_reason` are
    # strings this runtime composes from its own vocabulary. **Both were
    # wrong**, and an audit is what said so:
    #
    # - `provider_probe.py` stores `evidence={"exception": repr(exc)}`, and
    #   `repr` of an httpx error carries the request URL and the server's own
    #   response text. `lanes/manager.py` puts a payload's message straight in.
    # - `outcome_reason` is documented on `JobResult` as free plain language.
    #   `pipeline/runner.py` builds it as `f"unhandled exception: {exc!r}"`.
    #
    # `RunOutcome` is a closed vocabulary; the reason string riding beside it
    # is not, and that gap is the whole defect. So nothing is quotable today
    # except evidence a collector FORMATS ITSELF from values it already holds
    # — two timestamps, a count — and the bar for adding one is that no
    # substring of it came from outside this process.
    #
    # The general question, whether a provider's own words may be forwarded
    # at all, is the operator's and is not settled by this flag.
    # The answer that costs nothing is a local narration model, at which point
    # the gate is moot rather than tightened.
    quotable: bool = False
    # What the narration model is given in place of `summary`, when the
    # summary carries text this runtime did not choose. Empty means the
    # summary is already safe to forward, which is true of every finding
    # whose summary is built from counts and names.
    #
    # `quotable` gates the evidence lines; this gates the summary line. Both
    # exist because the operator's copy and the model's copy of a finding are
    # not the same artifact and must not be forced to be.
    model_summary: str = ""
    # Whether the judge may drop this because the runtime was still starting
    # up, or because the machine was away, and where it may not, why.
    #
    # Two fields rather than one because the two windows do not reach the same
    # findings: start-up is where this runtime blocks its own loop, so a stall
    # inside a boot is ordering rather than a fault, while a worker that died
    # is never start-up ordering and is plainly explained by the machine
    # having been off. Anything a producer leaves undeclared stays in the
    # report.
    by_boot: str = UNDECLARED
    by_outage: str = UNDECLARED

    @property
    def summary_for_model(self) -> str:
        return self.model_summary or self.summary

    @property
    def defect(self) -> bool:
        """Kept as a name because two places ask exactly this question: which
        findings get an evidence report a person can hand upstream, and which
        get counted as damage. Both mean `bad` and neither means `warn`."""
        return self.severity == BAD

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"finding {self.kind!r} declares severity {self.severity!r}, "
                f"which is not one of {', '.join(SEVERITIES)}"
            )
        if self.count < 1:
            raise ValueError(f"finding {self.kind!r} counts {self.count}")
        if not self.summary.strip():
            raise ValueError(f"finding {self.kind!r} has no summary")
        object.__setattr__(
            self,
            "evidence",
            tuple(line[:MAX_EVIDENCE_CHARS] for line in self.evidence[:MAX_EVIDENCE_LINES]),
        )


@dataclass(frozen=True)
class SourceRead:
    """One place the watchman looked, and what it saw there.

    `present=False` means the source does not exist on this machine — no
    directory, no file. That is `not_instrumented` to the surface and is
    rendered differently from a source that was read and was quiet.
    """

    name: str
    present: bool
    scanned: int = 0
    findings: tuple[Finding, ...] = ()
    # Why the read produced nothing usable, when it did — an unreadable file,
    # a permission error. A source that could not be read is not a quiet one.
    error: str = ""

    @property
    def quiet(self) -> bool:
        return self.present and not self.findings and not self.error


@dataclass(frozen=True)
class Sweep:
    """Every source, one window, one moment."""

    window_start: datetime | None
    window_end: datetime
    reads: tuple[SourceRead, ...] = field(default_factory=tuple)

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(f for read in self.reads for f in read.findings)

    @property
    def defects(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.defect)

    @property
    def unread(self) -> tuple[SourceRead, ...]:
        """Sources that are absent or errored — the ones a summary must not
        describe as quiet."""
        return tuple(r for r in self.reads if not r.present or r.error)


__all__ = [
    "MAX_EVIDENCE_CHARS",
    "MAX_EVIDENCE_LINES",
    "Finding",
    "SourceRead",
    "Sweep",
]
