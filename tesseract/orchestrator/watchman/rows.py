"""What the schedule rows did — read from the run log, judged against the schedule.

`manifest/checks.py` proves the declared set is *correct* at boot: a shipped row
nobody declares refuses to start. Nothing proved it was *running* afterwards.
`runs.jsonl` carries every fire and its outcome and was read by the Schedule tab
and the `schedule_list` tool, by nothing that reports — so a row that silently
stopped firing, or failed every night, produced no finding and reached neither
Telegram nor the brief.

One read, two artifacts: the watchman's ninth source turns these states into
findings, and the tracker renders the same states as a file the operator can
read. Neither of them re-derives what the other already knows.

**Late is judged against a row's own cadence.** A `*/5` row is late in minutes
and the anchor is late in hours, and a fixed poll answers the wrong question for
both. What the grace bounds is the two ends: a five-minute gap is inside the
noise of a busy tick, and a row that fires once a day cannot wait a day to be
called stopped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.orchestrator.outcome import (
    HEALTHY_OUTCOMES,
    RunOutcome,
    outcome_from_ok,
)
from tesseract.scheduler.cadence import next_fire, parse_interval
from tesseract.scheduler.log import iter_runs, runs_path

log = logging.getLogger(__name__)

# Bounds on the grace a row gets past its own next fire before it is late.
GRACE_MIN_S = 15 * 60
GRACE_MAX_S = 6 * 3600

# Outcomes that earn an evidence report the operator can hand upstream. A
# refusal is policy working (a breaker open, a pause) and a truncation resumes
# from its own watermark, so both are reported and neither is filed.
DEFECT_OUTCOMES = frozenset({
    RunOutcome.FAILED,
    RunOutcome.DEGRADED,
    RunOutcome.SKIPPED_UPSTREAM_FAILED,
})
_DEFECT_VALUES = frozenset(o.value for o in DEFECT_OUTCOMES)
_HEALTHY_VALUES = frozenset(o.value for o in HEALTHY_OUTCOMES)


@dataclass(frozen=True)
class RowState:
    """One row, and whether it is doing what it says it does."""

    name: str
    enabled: bool
    # How it fires, in the words the tracker prints: `every 5 min`, `daily at
    # 23:00`, `on provider_failover`.
    fires: str
    # True when the app ships this row — the manifest declares it. False means
    # the operator wrote it, and the ownership boundary says it is theirs.
    declared: bool
    summary: str
    last_run: datetime | None
    last_outcome: str
    # How far past its own next fire it is, or `None` when it is on time, has
    # no clock, or is disabled.
    late_by: timedelta | None
    never_ran: bool
    # `(outcome, reason)` for every run in the window that ended unhealthy.
    unhealthy: tuple[tuple[str, str], ...]
    runs_in_window: int

    @property
    def defective(self) -> bool:
        return any(outcome in _DEFECT_VALUES for outcome, _ in self.unhealthy)


@dataclass(frozen=True)
class RowReport:
    rows: tuple[RowState, ...]
    scanned: int
    # False when nothing has ever run on this machine. Absent is not quiet:
    # a first boot has no run log and no row has failed to fire.
    log_present: bool


def describe(cadence: str, when: str) -> str:
    """How a row fires, for someone who does not read cron.

    Three shapes are recognised because three shapes ship; anything else prints
    verbatim, which is honest rather than guessed at.
    """
    if when.strip():
        return f"on {when.strip()}"
    cadence = cadence.strip()
    if parse_interval(cadence):
        return f"every {cadence}"
    fields = cadence.split()
    if len(fields) == 5:
        minute, hour, dom, month, dow = fields
        if dom == month == dow == "*":
            if hour == "*" and minute.startswith("*/") and minute[2:].isdigit():
                return f"every {int(minute[2:])} min"
            if hour == "*" and minute.isdigit():
                return f"hourly at :{int(minute):02d}"
            if hour.isdigit() and minute.isdigit():
                return f"daily at {int(hour):02d}:{int(minute):02d}"
    return cadence


def _period_seconds(cadence: str, now: datetime) -> float | None:
    """The gap between two consecutive fires. `None` for a cadence nothing can read.

    Measured rather than pattern-matched: two fires apart is the period of any
    cadence, however it is spelled.
    """
    seconds = parse_interval(cadence)
    if seconds:
        return float(seconds)
    first = next_fire(cadence, now)
    second = next_fire(cadence, first) if first is not None else None
    if first is None or second is None:
        log.warning("watchman: unreadable cadence %r", cadence)
        return None
    return (second - first).total_seconds()


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(
        tzinfo=timezone.utc
    )


def _outcome_of(row: dict[str, Any]) -> str:
    """Rows written before the vocabulary existed carry `ok` and no outcome."""
    stated = row.get("outcome")
    if isinstance(stated, str) and stated.strip():
        return stated.strip()
    return outcome_from_ok(bool(row.get("ok"))).value


@dataclass
class _LogScan:
    """What one pass over `runs.jsonl` learned, per row name."""

    last_run: dict[str, datetime] = field(default_factory=dict)
    last_outcome: dict[str, str] = field(default_factory=dict)
    unhealthy: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    in_window: dict[str, int] = field(default_factory=dict)
    # The oldest fire in the log. Kept because it says how much history is on
    # disk; it is NOT what judges a row that has never fired — retention prunes
    # this file nightly, so it would move under the judgement.
    earliest: datetime | None = None
    scanned: int = 0


def _scan_log(path: Path, start: datetime | None, end: datetime) -> _LogScan:
    """One pass over the run log, through the reader that owns its format."""
    scan = _LogScan()
    last_run = scan.last_run
    last_outcome = scan.last_outcome
    unhealthy = scan.unhealthy
    in_window = scan.in_window
    earliest = scan.earliest
    scanned = 0

    healthy = _HEALTHY_VALUES
    for row in iter_runs(path):
        scanned += 1
        name = str(row.get("job_name") or "")
        fired = _parse_ts(row.get("fired_at"))
        if not name or fired is None:
            continue
        if earliest is None or fired < earliest:
            earliest = fired
        previous = last_run.get(name)
        if previous is None or fired > previous:
            last_run[name] = fired
            last_outcome[name] = _outcome_of(row)
        if (start is not None and fired <= start) or fired > end:
            continue
        in_window[name] = in_window.get(name, 0) + 1
        outcome = _outcome_of(row)
        if outcome in healthy:
            continue
        reason = str(row.get("outcome_reason") or row.get("detail") or "").strip()
        unhealthy.setdefault(name, []).append((outcome, reason))
    scan.earliest = earliest
    scan.scanned = scanned
    return scan


def boot_time() -> datetime | None:
    """When this process came up, read back from the boot id it already mints.

    `bootid` stamps `boot-YYYYMMDDTHHMMSS-<hex>` in UTC and the backend log is
    named after it, so the fact is already on disk. Reading it back beats
    adding a second notion of when the runtime started.
    """
    from tesseract.bootid import current_boot_id

    parts = current_boot_id().split("-")
    try:
        return datetime.strptime(parts[1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


def _declared_rows() -> dict[str, Any]:
    """Manifest entries that are rows or triggers, keyed by name.

    Not `BY_NAME`: that merges all four entry kinds into one namespace, and
    nothing stops an operator naming their own job after a service or after
    `scheduled_task`. Such a row would then print among the app's, under the
    manifest's unrelated sentence, with the operator's own summary discarded.
    `manifest/checks.py::check_rows` scopes itself the same way, for the same
    reason.
    """
    from tesseract.scheduler.manifest import Runs, entries_of

    return {e.name: e for e in (*entries_of(Runs.ROW), *entries_of(Runs.TRIGGER))}


def _lateness(
    cadence: str,
    *,
    last: datetime | None,
    now: datetime,
    running_since: datetime | None,
) -> timedelta | None:
    """How far past due a row is, or `None` if it is not.

    **A row cannot be late for the hours the machine was off.** The deadline is
    the LATER of its own next fire and one period after the app came up, so
    both have to be past before anything is reported. A machine switched off at
    20:00 and back on at 08:00 would otherwise open every morning with a
    Telegram defect report about the operator's own bedtime — and the true case,
    a row that stopped while the runtime was up, would arrive in the same
    channel as that noise.

    A row that still has not fired one period after boot IS late and says so,
    including one that has never fired at all. That is what keeps this from
    being a way to never report anything.
    """
    period = _period_seconds(cadence, now)
    if period is None:
        return None
    deadlines = []
    if last is not None and (due := next_fire(cadence, last)) is not None:
        deadlines.append(due)
    if running_since is not None:
        deadlines.append(running_since + timedelta(seconds=period))
    if not deadlines:
        return None
    deadline = max(deadlines)
    grace = timedelta(seconds=min(max(period, GRACE_MIN_S), GRACE_MAX_S))
    return now - deadline if now > deadline + grace else None


def read_rows(
    *,
    now: datetime,
    window_start: datetime | None,
    running_since: datetime | None = None,
    config_dir: Path | None = None,
    schedule_log_dir: Path | None = None,
) -> RowReport:
    """Every row in the live schedule, and what the run log says about it.

    `running_since` is when this process came up, defaulting to the boot id's
    own stamp. Nothing fires while the app is down, so lateness is judged from
    that moment on.
    """
    from tesseract import paths
    from tesseract.scheduler.config_loader import load_schedule_config

    schedule = load_schedule_config(config_dir or paths.config_dir())
    path = runs_path(schedule_log_dir or paths.log_dir("schedule"))
    present = path.exists()
    # The schedule alone answers "what runs"; only "did it run" needs the log.
    # Returning early on a missing one made a fresh install's tracker say
    # nothing was armed, when everything was and none of it had fired yet.
    scan = _scan_log(path, window_start, now) if present else _LogScan()
    since = running_since if running_since is not None else boot_time()
    declared = _declared_rows()

    states: list[RowState] = []
    for job in schedule.jobs:
        entry = declared.get(job.name)
        last = scan.last_run.get(job.name)
        late: timedelta | None = None
        if job.enabled and job.cadence.strip():
            late = _lateness(job.cadence, last=last, now=now, running_since=since)
        states.append(RowState(
            name=job.name,
            enabled=job.enabled,
            fires=describe(job.cadence, job.when),
            declared=entry is not None,
            # The app's rows say what they are for in the manifest; an
            # operator's row says it in the row. Neither is restated here,
            # which is what keeps this from becoming a third list of what runs.
            summary=entry.summary if entry is not None else job.summary.strip(),
            last_run=last,
            last_outcome=scan.last_outcome.get(job.name, ""),
            late_by=late,
            never_ran=last is None,
            unhealthy=tuple(scan.unhealthy.get(job.name, ())),
            runs_in_window=scan.in_window.get(job.name, 0),
        ))
    return RowReport(rows=tuple(states), scanned=scan.scanned, log_present=present)


__all__ = [
    "DEFECT_OUTCOMES",
    "boot_time",
    "GRACE_MAX_S",
    "GRACE_MIN_S",
    "RowReport",
    "RowState",
    "describe",
    "read_rows",
]
