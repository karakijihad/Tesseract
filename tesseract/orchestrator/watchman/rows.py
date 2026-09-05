"""What the schedule rows did — read from the run log, judged against the schedule.

`manifest/checks.py` proves the declared set is *correct* at boot: a shipped row
nobody declares refuses to start. Nothing proved it was *running* afterwards.
`runs.jsonl` carries every fire and its outcome and was read by the schedule view
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

from tesseract.orchestrator.outcome import HEALTHY_OUTCOMES, RunOutcome
from tesseract.scheduler.cadence import next_fire, parse_interval
from tesseract.scheduler.log import iter_runs, outcome_of_row, runs_path

log = logging.getLogger(__name__)

# Bounds on the grace a row gets past its own next fire before it is late.
GRACE_MIN_S = 15 * 60
GRACE_MAX_S = 6 * 3600

# A silence this many times the busiest row's own period means the scheduler
# was not ticking. Three, because one missed fire is a slow run and two is a
# machine that hiccuped, while three consecutive misses of the FASTEST row on
# the machine is a runtime that was not there.
STALL_PERIODS = 3
# Below this, a gap is not evidence of anything: a schedule whose busiest row
# is a minute apart would otherwise call three quiet minutes an outage.
STALL_MIN_S = 30 * 60
# How many fires may sit inside one outage before it stops being one. A
# machine that wakes for a single job on the way past is still a machine that
# was away; a scheduler that fired seven times across the window was there.
MAX_STALL_STIRS = 1

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
    # Where this row's messages go, in the words the tracker prints. Empty
    # when the row said nothing and the kind's own routing decides, which is
    # what every row starts as.
    reports_to: str = ""

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
    # `(from, to)` for every window in which nothing fired at all. Reported as
    # one outage rather than as a defect per row, because it is one event.
    stalls: tuple[tuple[datetime, datetime], ...] = ()


def reports_to(delivery: list[str] | None) -> str:
    """Where a row's messages go, for the tracker's own column.

    Three answers, and the operator has to be able to tell the last two apart:
    a row that said nothing follows the kind, a row that named nowhere is a
    decision they made, and neither is the same as naming a channel.
    """
    if delivery is None:
        return ""
    names = [str(name).strip() for name in delivery if str(name).strip()]
    return ", ".join(names) if names else "nowhere"


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
    """How one logged run went, read by the module that owns the row."""
    return outcome_of_row(row).value


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
    # `(when, row name)` for every fire in the log. This is the only record
    # of whether the scheduler was TICKING, which is a different question from
    # whether the process was up: it outlives a machine suspend, and a row
    # cannot be late for minutes in which nothing at all fired. The name is
    # what lets a gap be judged against the rows that span it.
    fires: list[tuple[datetime, str]] = field(default_factory=list)


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
        scan.fires.append((fired, name))
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


def _stalls(
    fires: list[tuple[datetime, str]], *, periods: dict[str, float]
) -> list[tuple[datetime, datetime]]:
    """Windows in which the scheduler fired nothing at all.

    A machine that sleeps takes its scheduler with it and leaves the process
    alive, so `running_since` sees no restart and every row wakes up hours
    past due. On 2026-08-20 that produced *"the watchman row is 11.0h past its
    next fire"* while `capture`, which fires every five minutes, had exactly
    the same eleven hour gap. A defect that appears in every row at the same
    instant is the machine, not the rows.

    **The threshold is per gap, from the rows that fired on BOTH sides of
    it.** A global threshold taken from the fastest row on the schedule reads
    the log wrong the moment that row is the one that dies: with `capture`
    stopped, every hourly `watchman` gap exceeded it, each stall ended where
    the next began, the merge rule joined them, and the runtime reported
    itself asleep every hour while plainly running. A row that fired before
    the gap and again after it is proof of what the scheduler does when it is
    working, and a row that stopped is not spanning anything.

    A gap no row spans is left alone: nothing can say whether that was an
    outage or a runtime that quietly stopped, and the second is the one that
    must never be silenced.
    """
    if not fires:
        return []
    ordered = sorted(fires)
    when: dict[str, list[datetime]] = {}
    for moment, name in ordered:
        when.setdefault(name, []).append(moment)

    # 1. Every silence long enough to be worth asking about.
    candidates = [
        (earlier, later)
        for (earlier, _a), (later, _b) in zip(ordered, ordered[1:])
        if (later - earlier).total_seconds() > STALL_MIN_S
    ]

    # 2. Joined, before anything is judged. A machine that wakes for one job
    # and sleeps again produced two silences around a single fire: the
    # 2026-08-20 outage read as 21:20 to 03:02 and 03:02 to 08:50, because
    # `janitor_sweep` runs on uptime rather than a clock and fired alone on
    # the way past. That is one outage with a stir in the middle of it.
    #
    # This has to happen BEFORE step 3, not after: neither half of a split
    # outage is bracketed by the row that proves the machine was working,
    # because that row is on the far side of the other half.
    merged: list[tuple[datetime, datetime]] = []
    for began, ended in candidates:
        if merged and (began - merged[-1][1]).total_seconds() <= STALL_MIN_S:
            merged[-1] = (merged[-1][0], ended)
        else:
            merged.append((began, ended))

    # 3. Kept only where a row with a CLOCK was working on both sides of it.
    # "Fired somewhere before and somewhere after" is too weak: a daily row
    # brackets the whole log and so spans every silence inside it, which would
    # let one push the bar past a real outage. Near both edges is the claim.
    def spans(name: str, period: float, began: datetime, ended: datetime) -> bool:
        reach = timedelta(seconds=max(period * STALL_PERIODS, STALL_MIN_S))
        times = when.get(name, ())
        return (
            any(began - reach <= t <= began for t in times)
            and any(ended <= t <= ended + reach for t in times)
        )

    # 4. And only where the scheduler was not demonstrably running THROUGH it.
    # Merging in step 2 is what makes this necessary: with the densest row
    # dead, every gap of a surviving hourly row clears the floor and each ends
    # where the next begins, so they merge into one unbroken window and the
    # runtime would report itself asleep every hour while plainly working.
    # One stir inside a silence is a machine waking for a job. Several are a
    # scheduler keeping time, and whatever is wrong then is a row, not the
    # machine, which must be reported rather than explained away.
    def stirs(began: datetime, ended: datetime) -> int:
        return sum(1 for t, _n in ordered if began < t < ended)

    return [
        (began, ended) for began, ended in merged
        if stirs(began, ended) <= MAX_STALL_STIRS
        and any(spans(name, period, began, ended) for name, period in periods.items())
    ]


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

    # Only rows with a CLOCK. A trigger row fires when something happens, so
    # its silence measures nothing and its lone fire is not evidence the
    # scheduler kept its own time.
    periods = {
        job.name: p for job in schedule.jobs
        if job.enabled and job.cadence.strip()
        and (p := _period_seconds(job.cadence, now)) is not None
    }
    stalls = _stalls(scan.fires, periods=periods) if present else []
    # A row is judged from the end of the last stall, for the same reason it is
    # judged from boot: nothing fired before it, and nothing that did not fire
    # can be late.
    if stalls:
        since = max(since, stalls[-1][1]) if since is not None else stalls[-1][1]
    # Lateness is judged from the newest stall whenever it happened; only the
    # ones that ENDED in this window are reported, or every pass would re-tell
    # the operator about every night the machine has ever been off.
    reported_stalls = [
        (began, ended) for began, ended in stalls
        if window_start is None or ended > window_start
    ]

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
            reports_to=reports_to(job.delivery),
        ))
    return RowReport(rows=tuple(states), scanned=scan.scanned, log_present=present,
                     stalls=tuple(reported_stalls))


__all__ = [
    "DEFECT_OUTCOMES",
    "boot_time",
    "reports_to",
    "GRACE_MAX_S",
    "GRACE_MIN_S",
    "RowReport",
    "RowState",
    "describe",
    "read_rows",
]
