"""A day the daily pass did not complete.

Every other schedule source reads whether a ROW fired. This one reads whether
the STAGES inside it got their turn, which is a different question with a
different answer: `read_schedule` sees the `consolidate` row run at 17:00 and
report `ok`, and a run whose stages were every one of them `not_due` reports
exactly the same. Nothing on this machine asserted that the pass had actually
completed, so a fortnight of `skipped_no_work` was indistinguishable from a
fortnight of work.

**Read off the watermarks, because the watermark is what the runner decides
on.** `runner.is_due` compares the anchor against `WatermarkStore.get(stage)`,
so the same value that decides whether a stage runs decides whether it is
overdue. A second clock here would be a second opinion about the same fact.

**A stage is stale only when it has missed a WHOLE cycle.** Not `period` and
not `period - slack`, which is the runner's due test and fires every day by
design a few hours before the anchor. Two periods is the first number that
cannot be explained by a normal rhythm on a machine that is off at night: a
daily stage that ran yesterday afternoon is 24 hours behind at the same time
today and is not a fault, and one that has not run in 48 is a day that did
not happen.

**A machine that was away explains it, and says so rather than being silent.**
The catch-up replays a missed anchor at the next boot, so between opening the
app and the pass finishing every daily stage is legitimately behind. That is
`by_boot` and `by_outage`, declared `EXPLAINABLE` so the judge can spend a
real absence on it, which is the whole reason those fields exist.

Never raises: an unreadable watermark file is no reading, not a fault.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from tesseract.orchestrator.watchman.findings import (
    BAD,
    EXPLAINABLE,
    Finding,
    SourceRead,
)

log = logging.getLogger(__name__)

#: How many of a stage's own periods may pass before it is a finding. One is
#: the runner's due test, which is true for a few hours every day on purpose.
#: Two is the first count that means a cycle was actually missed.
STALE_AFTER_PERIODS = 2

#: The one remedy, named here so item 5's table and the operator read the same
#: sentence. It runs one stage by hand and is the whole fix.
REMEDY = "pipeline_run_stage"

#: How many stage names the sentence carries before it says "and N more".
_NAMES_IN_SUMMARY = 8


def read_pipeline(start: datetime | None, end: datetime) -> SourceRead:
    """Which declared stages have missed a whole cycle."""
    from tesseract.scheduler.pipeline.artifacts import WatermarkStore
    from tesseract.scheduler.pipeline.runner import _CADENCE_PERIOD
    from tesseract.scheduler.pipeline.stages import CAPTURE_ROW, CONSOLIDATE_ROW

    store = WatermarkStore()
    stale: list[tuple[str, str, timedelta]] = []
    seen = 0
    ran_at_all = False

    for row in (CONSOLIDATE_ROW, CAPTURE_ROW):
        for stage in row.stages:
            period = _CADENCE_PERIOD.get(stage.cadence)
            if not period:
                # A continuous stage has no cycle to miss.
                continue
            seen += 1
            last = store.get(stage.name)
            if last is None:
                # Never run. On a machine where nothing has ever run that is
                # a fresh install, not a fault, and the check below is what
                # tells the two apart.
                continue
            ran_at_all = True
            # A watermark is the ANCHOR a run was for, not the moment it
            # finished, and an anchor can be later today than now: on this
            # machine `daily_writer` reads five hours ahead. So this span goes
            # negative in normal operation and the comparison below has to be
            # one that a negative never satisfies.
            behind = end - last
            if behind >= period * STALE_AFTER_PERIODS:
                stale.append((row.name, stage.name, behind))

    if not seen or not ran_at_all:
        # Nothing has ever run here. Not the same as everything being late,
        # and the same answer `read_schedule` gives to an empty run log.
        return SourceRead(name="pipeline", present=False)

    if not stale:
        return SourceRead(name="pipeline", present=True, scanned=seen)

    worst = max(behind for _, _, behind in stale)
    # `Finding` keeps the first MAX_EVIDENCE_LINES and drops the rest without
    # saying so, so the summary carries the names and is bounded here rather
    # than being allowed to outrun the evidence under it. The common case is
    # one or two stale stages; the case that needs bounding is a machine that
    # was off for a week, which `by_outage` already explains.
    ordered = sorted(name for _, name, _ in stale)
    names = ", ".join(ordered[:_NAMES_IN_SUMMARY])
    if len(ordered) > _NAMES_IN_SUMMARY:
        names += f" and {len(ordered) - _NAMES_IN_SUMMARY} more"
    return SourceRead(
        name="pipeline",
        present=True,
        scanned=seen,
        findings=(
            Finding(
                source="pipeline",
                kind="pass_incomplete",
                # The pass as a whole, not one stage: the remedy is per stage
                # but the fault is that the day did not settle, and a subject
                # per stage would announce five faults for one missed night.
                subject="the daily pass",
                summary=(
                    f"{len(stale)} of {seen} stages of the daily pass have not "
                    f"run in {_days(worst)}: {names}. Run one with "
                    f"`{REMEDY}`"
                ),
                last_at=end,
                # Worst first, because the cap keeps the head of the list.
                evidence=tuple(
                    f"{row}/{stage}: last ran {_days(behind)} ago"
                    for row, stage, behind in sorted(stale, key=lambda s: -s[2])
                ),
                # A real absence explains this, and the catch-up at the next
                # boot is what fixes it, so both are spendable here.
                by_boot=EXPLAINABLE,
                by_outage=EXPLAINABLE,
                severity=BAD,
                quotable=True,
            ),
        ),
    )


def _days(span: timedelta) -> str:
    """How long, rounded DOWN.

    A fault report may not overstate itself. Rounding 3.8 days to "4 days"
    tells the operator a stage missed four cycles when it missed three, and
    the number is the whole of what they act on.
    """
    hours = span.total_seconds() / 3600
    if hours < 48:
        return f"{int(hours)} hours"
    return f"{int(hours // 24)} days"


__all__ = ["REMEDY", "STALE_AFTER_PERIODS", "read_pipeline"]
