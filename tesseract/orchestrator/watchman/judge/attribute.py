"""Stage 3 — ours or theirs, inside a boot, inside an outage?

Two adjacent facts, uncorrelated, is what produced the message this phase was
written about. The report said `brief_push: send_text failed` and, in its own
narration two lines later, that the backend had started eighteen times. The
error was logged at 14:18:05. The restart was at 14:15:51. Nothing in the
runtime connected them, so a component that had not finished coming up was
reported as a component that had broken.

**A fault inside a boot window is startup ordering.** An error logged within
`BOOT_WINDOW` of a `boot:` line, from a component that then came up, is the
runtime starting in an order it did not like. It is reported as that, or not
at all. The distinction matters because the two have different remedies: a
startup-ordering artifact is fixed by the boot graph, and a fault is fixed by
whoever owns the component.

**Downtime is a state, not a defect.** `rows.py::_stalls` replays a machine
that slept as one outage with zero late rows, which settles it for the ROWS.
This stage reads those same windows and applies them to everything else: a
worker that died while the machine was suspended, an error logged as the
network went away with it. A fault that begins and ends inside a window in
which the scheduler fired nothing is the machine going away, and it is
reported as that or not at all — the same rule as a boot, on a different
piece of evidence.

**"Reported as that, or not at all"** is the exit criterion, and it means
dropped from the message rather than softened inside it. A finding kept with a
start-up reason attached still reads as a fault wherever its summary is
printed, and the reason only exists in the verdict. So the finding leaves the
report and the verdict says why, which the summary prints under *What was
judged away* — reported as start-up ordering, in the place a person goes to
check what the judge did, and not in the line that asks them to act.

Keeping it also had a second cost the tests found: a start-up artifact that
survived this stage entered stage 4's standing store and, an hour later,
announced a recovery from a fault it never had.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from tesseract.orchestrator.watchman.findings import EXPLAINABLE

if TYPE_CHECKING:
    from tesseract.orchestrator.watchman.findings import Finding
    from tesseract.orchestrator.watchman.judge import Verdict

log = logging.getLogger(__name__)

STAGE = "attribute"

# How long after a `boot:` line an error still counts as the runtime starting
# up rather than failing. Measured against the case this stage exists for: the
# telegram bridge error landed 2m14s after its boot line. Three minutes covers
# that with room, and is short enough that a fault arriving ten minutes into a
# healthy process is still a fault.
BOOT_WINDOW = timedelta(minutes=3)


# WHICH findings each window may account for is no longer decided here. Every
# producer declares it on the finding, as `by_boot` and `by_outage`, and this
# stage only applies the two windows.
#
# It used to be two frozen sets of kind strings, and they could not be right.
# The kinds are not enumerable: `read_supervisor` passes an incident's own
# `event` string through as the kind, so no list written here can be complete.
# And "not in the list" flattened three different reasons into one absence, so
# a verdict could never say why something was left alone. `findings.py` holds
# the vocabulary and the reasoning behind each value.


def apply(verdicts: list["Verdict"], *, now: datetime) -> list["Verdict"]:
    from tesseract.orchestrator.watchman.judge import DROPPED, Verdict

    boots = _boot_times(now)
    outages = _outage_windows(now)
    out: list[Verdict] = []
    for verdict in verdicts:
        if not verdict.kept:
            out.append(verdict)
            continue
        boot = _boot_that_explains(verdict.finding, boots)
        if boot is not None:
            out.append(Verdict(
                finding=verdict.finding,
                stage=STAGE,
                action=DROPPED,
                why=(
                    f"logged {_gap(verdict.finding, boot)} after the runtime started at "
                    f"{boot.isoformat(timespec='seconds')}, so this is start-up ordering "
                    f"rather than a fault"
                ),
            ))
            continue
        outage = _outage_that_explains(verdict.finding, outages)
        if outage is not None:
            began, ended = outage
            out.append(Verdict(
                finding=verdict.finding,
                stage=STAGE,
                action=DROPPED,
                why=(
                    f"happened while nothing was running, between "
                    f"{began.isoformat(timespec='seconds')} and "
                    f"{ended.isoformat(timespec='seconds')}, so this is the machine "
                    f"being away rather than a fault"
                ),
            ))
            continue
        out.append(verdict)
    return out


def _gap(finding: "Finding", boot: datetime) -> str:
    when = finding.first_at or finding.last_at
    if when is None:
        return "moments"
    seconds = int((when - boot).total_seconds())
    return f"{seconds}s" if seconds < 120 else f"{seconds // 60}m"


def _boot_that_explains(finding: "Finding", boots: list[datetime]) -> datetime | None:
    """The boot this finding started inside, if it did.

    Judged on FIRST occurrence, not last. A fault that began during start-up
    and is still happening an hour later is not start-up ordering, and reading
    the last timestamp would say it was.
    """
    if finding.by_boot != EXPLAINABLE:
        return None
    started = finding.first_at or finding.last_at
    if started is None:
        return None
    # A finding that spans more than one boot window is recurring, not a
    # single restart's noise.
    inside = [b for b in boots if b <= started <= b + BOOT_WINDOW]
    if not inside:
        return None
    last = finding.last_at or started
    if last > inside[-1] + BOOT_WINDOW:
        return None
    return inside[-1]


def _outage_that_explains(
    finding: "Finding", outages: list[tuple[datetime, datetime]]
) -> tuple[datetime, datetime] | None:
    """The outage this finding began and ended inside, if there is one.

    Both ends, for the reason a boot needs both: something that started while
    the machine was away and is still going now is a fault the outage merely
    happens to precede.
    """
    if finding.by_outage != EXPLAINABLE:
        return None
    started = finding.first_at or finding.last_at
    if started is None:
        return None
    last = finding.last_at or started
    for began, ended in outages:
        if began <= started and last <= ended:
            return (began, ended)
    return None


def _outage_windows(now: datetime) -> list[tuple[datetime, datetime]]:
    """Every window the runtime was not there for.

    Two readings, and the machine's own comes first. Windows records when it
    went to standby, when a shutdown was asked for and by whom, and whether the
    last one was clean, so *was the computer even on* is a fact to look up
    rather than a shape to infer. Before this the only answer came from the
    scheduler's own log, where a machine asleep and a scheduler that stopped
    firing are the same silence.

    The inferred windows are kept underneath rather than replaced. They cover
    what the power record cannot: a runtime that was down while the machine
    stayed up. The two halves do not reach equally far back, and the docstring used to
    claim they did. THIS call's machine read is bounded to
    `machine_state.LOOKBACK`; the inferred half reads the whole run log.
    The collector in `sources.py` reads the same record on its own terms
    and can go further back than this one does, so the bound is a fact
    about this call rather than about the record. `read_rows` with no window start is what makes an outage that
    began before the sweep window usable here, since a machine off overnight
    has one window and the report is written the morning after, inside it.
    """
    windows: list[tuple[datetime, datetime]] = []
    try:
        from tesseract.orchestrator import machine_state

        found = machine_state.gaps(now - machine_state.LOOKBACK, now=now)
        windows.extend((gap.began, gap.ended) for gap in (found or ()))
    except Exception:  # noqa: BLE001 — the log's own answer still stands below
        log.debug("judge: the machine's power record could not be read", exc_info=True)
    try:
        from tesseract.orchestrator.watchman.rows import read_rows

        windows.extend(read_rows(now=now, window_start=None).stalls)
    except Exception:  # noqa: BLE001 — a stage that cannot read explains nothing
        log.warning("judge: the run log could not be read; explaining no outages",
                    exc_info=True)
    return windows


def blind_spots(sweep, *, now: datetime) -> list[str]:
    """Where this stage could not see, said in the reader's own words.

    The boot rule reads the per-boot log FILENAMES, and `retention.yaml`
    prunes them. Observed inside one session: a correlation that fired at
    19:00 did not fire at 19:30 because the log had rotated. The rule then
    goes quiet exactly the way a working rule does, and a start-up artifact
    reaches the operator as a fault with nothing saying why the judge let it
    through.

    So a sweep whose window reaches back further than the oldest boot log on
    disk says so, and the summary prints it. This is the cheap half of the
    fix; the other half is a boot ledger retention does not touch, which is a
    second copy of evidence the backend already writes.
    """
    window_start = getattr(sweep, "window_start", None)
    if window_start is None:
        return []
    boots = _boot_times(now)
    if not boots:
        return [
            "no boot log is on disk, so nothing in this window could be read as "
            "start-up ordering"
        ]
    oldest = boots[0]
    if oldest <= window_start:
        return []
    return [
        f"the boot log only reaches back to {oldest.isoformat(timespec='seconds')}, "
        f"so anything before that could not be read as start-up ordering"
    ]


def _boot_times(now: datetime) -> list[datetime]:
    """When each process came up, from the per-boot log files already on disk.

    `rows.boot_time()` answers for the CURRENT process only, and a window can
    span several restarts — eighteen of them, in the case this stage was
    written for. The backend names each log after the boot id it minted, so
    the times are already recorded and need no second notion of start-up.
    """
    try:
        from tesseract.orchestrator.watchman.sources import _boot_time
        from tesseract.paths import log_dir

        directory = log_dir("backend")
        if not directory.exists():
            return []
        times = [t for path in directory.iterdir() if (t := _boot_time(path))]
    except Exception:  # noqa: BLE001 — a stage that cannot read explains nothing
        log.warning("judge: boot times unreadable; attributing no start-up noise", exc_info=True)
        return []
    return sorted(t for t in times if t <= now)


__all__ = ["BOOT_WINDOW", "STAGE", "apply", "blind_spots"]
