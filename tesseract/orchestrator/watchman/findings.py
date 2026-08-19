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

# Evidence lines are quoted verbatim into a report the operator may hand
# upstream, so they are capped per finding rather than by total size: ten lines
# of one failure says what it is, and a thousand says nothing at all.
MAX_EVIDENCE_LINES = 10
MAX_EVIDENCE_CHARS = 400


@dataclass(frozen=True)
class Finding:
    """One counted thing that happened, with the lines that prove it."""

    source: str
    kind: str
    summary: str
    count: int = 1
    first_at: datetime | None = None
    last_at: datetime | None = None
    evidence: tuple[str, ...] = ()
    # True for a finding that describes a defect rather than a condition: a
    # worker that died with an error class, a breaker that tripped. These get
    # an evidence report the operator can hand upstream; a governor pause or a
    # quiet janitor sweep does not.
    defect: bool = False

    def __post_init__(self) -> None:
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
