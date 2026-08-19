"""What one piece of ordered background work declares about itself.

A stage declares its edges as data — what it reads, what it writes, how often,
how long it may take — so an ordering can be enforced and reported rather than
implied. `schedule.yaml` used to hold 31 independently-fired cron rows whose
ordering lived in clock minutes: `memory_scrub --fix` at 23:45 consumed findings
`memory_lint` wrote at 23:30, an edge nothing declared and nothing checked.
Eighteen of those rows are stages of two rows now, and that pair is the last
clock in the system.

Two kinds of edge, and the difference is load-bearing: `reads` is a data
dependency and cascades failure; `after` only orders. Most of the nightly set
reads canonical files rather than each other's output, so declaring those as
`reads` would invent both a dependency and a cascade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable

from tesseract.orchestrator.outcome import RunOutcome


class StageCadence(str, Enum):
    """How often a stage is due. Never a clock time: one anchor moves the
    whole pipeline, and a stage that owns a minute owns an ordering."""

    # Due on every invocation of its row. The capture row fires in minutes and
    # its stages decide what to do from their own thresholds, so "how often"
    # is the row's question there and not the stage's. Added in AR-3; the
    # contract's `daily`/`weekly` still govern the nightly row, where cadence
    # is the whole point.
    CONTINUOUS = "continuous"
    DAILY = "daily"
    WEEKLY = "weekly"


class StageKind(str, Enum):
    """`model` stages depend on a provider being reachable; `deterministic`
    ones do not. The runner keeps the second out of the first's way."""

    DETERMINISTIC = "deterministic"
    MODEL = "model"


class ProviderUnreachable(Exception):
    """Raised by a model stage that could not reach its provider.

    Distinct from any other exception on purpose: an unreachable provider is a
    `degraded` run, not a `failed` one, and must not cascade to the stages
    downstream of it.
    """


@dataclass(frozen=True)
class StageReport:
    """What a stage body returns. One outcome, and a reason for anything that
    is not plain success."""

    outcome: RunOutcome
    reason: str = ""
    changed: int = 0
    refused: int = 0
    # How far this run consumed its input. `None` means "up to the run's
    # anchor" — the common case for a stage whose input has no timeline of
    # its own. A stage that stopped early sets it so the next run resumes
    # from there rather than re-reading the whole window.
    watermark: datetime | None = None

    def __post_init__(self) -> None:
        if self.outcome is not RunOutcome.SUCCEEDED and not self.reason.strip():
            raise ValueError(
                f"stage outcome {self.outcome.value} needs a reason a person "
                "can read"
            )


@dataclass
class StageContext:
    """One stage's handle on one run.

    `read` and `write` are the enforcement point for the declared scope: a
    stage that asks for an artifact it did not declare gets an error, not a
    surprise dependency the graph cannot see.
    """

    stage: "Stage"
    run_id: str
    anchor: datetime
    # The window this run covers: everything between the stage's own watermark
    # and the anchor. A machine off for a week hands one stage one seven-day
    # window, which is what makes catch-up run once rather than seven times.
    window_start: datetime | None
    window_end: datetime
    artifacts: Any = None
    app: Any = None
    config: dict[str, Any] = field(default_factory=dict)
    # The manifest entry this stage runs under — the row's name. A stage is
    # not an entry of its own: it runs because the row does, so what it spends
    # is the row's spend and the ledger bills it there.
    entry: str = ""
    # The scheduler's own log directory, threaded for the stages whose bodies
    # read `runs.jsonl` (the daily writer does).
    log_dir: Any = None
    reads_seen: dict[str, int] = field(default_factory=dict)
    writes_made: dict[str, int] = field(default_factory=dict)

    def read(self, name: str) -> Any:
        """The current version of a declared input, or None if never written."""
        if name not in self.stage.reads:
            raise ValueError(
                f"stage {self.stage.name!r} read {name!r}, which is not in its "
                f"declared reads {list(self.stage.reads)}"
            )
        head = self.artifacts.head(name) if self.artifacts is not None else None
        if head is not None:
            self.reads_seen[name] = head.version
        return head

    def write(self, name: str, *, watermark: datetime | None = None) -> Any:
        """Publish a new version of a declared output."""
        if name not in self.stage.writes:
            raise ValueError(
                f"stage {self.stage.name!r} wrote {name!r}, which is not in its "
                f"declared writes {list(self.stage.writes)}"
            )
        if self.artifacts is None:
            raise RuntimeError(f"stage {self.stage.name!r} has no artifact store")
        version = self.artifacts.publish(
            name,
            produced_by=self.stage.name,
            watermark=watermark or self.window_end,
        )
        self.writes_made[name] = version.version
        return version


StageBody = Callable[[StageContext], Awaitable[StageReport]]


@dataclass(frozen=True)
class Stage:
    """A stage's static declaration. Everything here is known before boot, so
    the graph can be checked before anything runs."""

    name: str
    body: StageBody
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    # Ordering without dependency: "run after that one, but do not wait on its
    # result". Added in AR-3 batch 2, because most of the nightly set reads
    # canonical files rather than each other's output — the librarian
    # consolidates the daily layer whether or not the digest wrote to it, and
    # the memory maintenance chain must not be skipped because a model was
    # unreachable. Declaring those as `reads` would have been a false data
    # dependency AND a false cascade; declaring nothing would have put the
    # ordering back in the clock.
    after: tuple[str, ...] = ()
    cadence: StageCadence = StageCadence.DAILY
    kind: StageKind = StageKind.DETERMINISTIC
    budget_seconds: float = 300.0
    # In-run retries on `failed`. Zero for anything that calls a model — a
    # retry there re-bills the call, and the next anchor is the cheaper place
    # to try again. Non-zero only where a migrated job carried a retry and
    # retrying costs nothing but time.
    retries: int = 0
    retry_backoff_seconds: float = 0.0
    # True for a wrapped job that derives ONE calendar day from `fired_at`
    # rather than reading the window it is given. Declaration data, not a
    # closure detail: whether a stage can cover a gap in one call is a fact
    # about the graph, and it has to be answerable without running it.
    per_day: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a stage needs a name")
        if self.budget_seconds <= 0:
            raise ValueError(
                f"stage {self.name!r}: budget_seconds must be positive, "
                f"got {self.budget_seconds}"
            )
        overlap = set(self.reads) & set(self.writes)
        if overlap:
            raise ValueError(
                f"stage {self.name!r} both reads and writes {sorted(overlap)} — "
                "that is a self-edge the graph cannot order"
            )
        if self.name in self.after:
            raise ValueError(f"stage {self.name!r} cannot run after itself")
        if self.retries < 0:
            raise ValueError(f"stage {self.name!r}: retries cannot be negative")
        if self.retries and self.kind is StageKind.MODEL:
            raise ValueError(
                f"stage {self.name!r} calls a model and declares {self.retries} "
                "retries — a retry re-bills the call; let the next anchor try again"
            )


__all__ = [
    "ProviderUnreachable",
    "Stage",
    "StageBody",
    "StageCadence",
    "StageContext",
    "StageKind",
    "StageReport",
]
