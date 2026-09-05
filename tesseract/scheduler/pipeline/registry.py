"""The rows, and the check that they agree with each other.

A row is one schedule entry and the subgraph it runs. There are two by design —
capture, which fires in minutes and never reasons, and consolidate, which runs
once against the anchor. A third row would re-create the ordering problem this
plan is removing, so the shape is deliberately not open-ended.

A row may consume what another row produces (`imports`). Ordering is per row,
because the rows fire independently; the producer check is over all of them,
because "nothing writes this" is a question about the system.
"""

from __future__ import annotations

from dataclasses import dataclass

from tesseract.scheduler.pipeline.graph import (
    PipelineDeclarationError,
    execution_order,
    producers,
)
from tesseract.scheduler.manifest.entry import MIN_SUMMARY_CHARS
from tesseract.scheduler.pipeline.stage import Stage


@dataclass(frozen=True)
class Row:
    name: str
    stages: tuple[Stage, ...]
    # Artifacts produced by a DIFFERENT row. Declared rather than assumed: an
    # undeclared cross-row read is how an ordering nobody wrote down comes
    # back, one file further from `schedule.yaml` than last time.
    imports: tuple[str, ...] = ()

    @property
    def external_reads(self) -> frozenset[str]:
        return frozenset(self.imports)


_ROWS: dict[str, Row] = {}


def register_row(row: Row) -> Row:
    if row.name in _ROWS:
        raise ValueError(f"row {row.name!r} is already registered")
    _ROWS[row.name] = row
    return row


def rows() -> tuple[Row, ...]:
    return tuple(_ROWS.values())


def row(name: str) -> Row | None:
    return _ROWS.get(name)


def find_stage(name: str) -> tuple[Row, Stage] | None:
    for r in _ROWS.values():
        for stage in r.stages:
            if stage.name == name:
                return r, stage
    return None


def validate_rows(candidates: tuple[Row, ...] | None = None) -> None:
    """Every row orders, every import has a producer, every stage says what
    it does.

    The summary floor is here rather than on `Stage.__post_init__` for the
    reason the manifest puts its floor at `SchedulerEngine.start`: a test's
    throwaway stage is not a shipped stage. What is checked is the REGISTERED
    set — the work this repo ships — and a stage that cannot say what it does
    renders as a blank cell in the Guide and a nameless node on the floor
    plan.
    """
    checked = rows() if candidates is None else candidates
    written = producers(stage for r in checked for stage in r.stages)
    for r in checked:
        execution_order(r.stages, external_reads=r.external_reads)
        for stage in r.stages:
            if len(stage.summary.strip()) < MIN_SUMMARY_CHARS:
                raise PipelineDeclarationError(
                    f"stage {stage.name!r} in row {r.name!r} has a "
                    f"{len(stage.summary.strip())}-character summary — say what "
                    f"it does in at least {MIN_SUMMARY_CHARS}, in the language "
                    "an operator reads, because the Guide and the Autonomy "
                    "panel both render this and neither has anywhere else to look"
                )
        for name in r.imports:
            owners = [owner for owner in written.get(name, ()) if owner not in
                      {stage.name for stage in r.stages}]
            if not owners:
                raise PipelineDeclarationError(
                    f"row {r.name!r} imports {name!r}, which no other row writes"
                )


__all__ = [
    "Row",
    "find_stage",
    "register_row",
    "row",
    "rows",
    "validate_rows",
]
