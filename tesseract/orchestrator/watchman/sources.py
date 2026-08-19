"""What the runtime wrote down about itself, read directly.

The heartbeat this replaces read workspace events and memory writes — the
system's own bookkeeping — and so every observation it ever produced was about
its own bookkeeping. The logs, the breakers and the worker records that
describe a failure directly were never opened. These collectors open them.

Every one of them is deterministic and every one of them is cheap: counting
breaker trips and failed workers needs no model, and the model's only job
downstream is turning a counted set of facts into a sentence.

Three rules they all keep:

* **Paths resolve at call time.** `log_dir()` reads `TESSERACT_HOME` when it is
  called, so a test pointing that at `tmp_path` is read by the collector rather
  than by whatever the module saw at import.
* **A missing source is reported, not skipped.** `SourceRead.present` is what
  makes "there is no such directory on this machine" different from "nothing
  went wrong", which are the same empty list.
* **They never raise.** A collector that cannot read its own source returns the
  read with an `error`; one unreadable file must not cost the whole sweep.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from tesseract.orchestrator.outcome import HEALTHY_OUTCOMES, RunOutcome
from tesseract.orchestrator.watchman.findings import (
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_LINES,
    Finding,
    SourceRead,
    Sweep,
)

log = logging.getLogger(__name__)

# How far back a first run looks. After that the cursor decides, so this is
# only ever the width of the very first sweep on a machine.
DEFAULT_LOOKBACK_HOURS = 24

# A per-boot backend log is plain text, and a boot loop can produce a lot of
# it. Read the tail rather than the file: an error that appears only in the
# first 5 MB of a 200 MB log is not the one anybody is looking for.
BACKEND_TAIL_BYTES = 256 * 1024
_BACKEND_ERROR = re.compile(r"\b(ERROR|CRITICAL)\b")
# The changing parts of a log line — timestamps, ids, paths, numbers — removed
# so twelve occurrences of one fault count as twelve rather than as twelve
# separate faults.
_VOLATILE = re.compile(r"\d+|0x[0-9a-f]+", re.I)
# The log's own timestamp, dropped from the sentence the summary prints. The
# full line is kept as evidence; what the summary needs is the error, and a
# date at the front of it both reads as the finding's own time and puts four
# meaningless numbers into the set a narration is allowed to cite.
_LEADING_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*\s*")
# A summary is read on a phone, so it ends where a sentence ends. A character
# count ended it mid-word — "mapper is enabled but none of i" is what reached
# the operator — and half a word is not a report of anything. The full line
# stays in the evidence file either way.
SUMMARY_MAX_CHARS = 200
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _readable_summary(line: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """One whole sentence of a log line, or whole words and an ellipsis."""
    text = _LEADING_STAMP.sub("", line).strip()
    if len(text) <= limit:
        return text
    ends = [m.end() for m in _SENTENCE_END.finditer(text) if m.end() <= limit]
    if ends:
        return text[: ends[-1]].strip()
    cut = text.rfind(" ", 0, limit)
    return (text[:cut] if cut > 0 else text[:limit]).rstrip() + "…"


def _parse_ts(value: Any) -> datetime | None:
    """Every producer in this tree writes ISO 8601, and not one of them agrees
    on the spelling: `Z`, `+00:00`, and a local offset all appear. Naive
    timestamps are read as UTC rather than dropped — a log line with no zone is
    still evidence, and dropping it silently would make an old failure look new.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(
            tzinfo=timezone.utc
        )
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _in_window(ts: datetime | None, start: datetime | None, end: datetime) -> bool:
    if ts is None:
        return False
    if start is not None and ts <= start:
        return False
    return ts <= end


def _rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _guarded(name: str, read: Callable[[], SourceRead]) -> SourceRead:
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 — one bad source must not end the sweep
        log.exception("watchman: source %s failed", name)
        return SourceRead(name=name, present=True, error=f"{type(exc).__name__}: {exc}")


def _span(times: list[datetime]) -> tuple[datetime | None, datetime | None]:
    return (min(times), max(times)) if times else (None, None)


# ── the sources ─────────────────────────────────────────────────────


def read_breakers(start: datetime | None, end: datetime) -> SourceRead:
    """Trips inside the window, plus anything still open from before it.

    A breaker that tripped yesterday and is still open is today's problem too,
    which is why the open ones are reported whatever the window says.
    """
    from tesseract.paths import log_dir

    directory = log_dir("circuit-breakers")
    if not directory.exists():
        # Not a defect: nothing has ever tripped on this machine, so no
        # producer has created the directory. Absent, and said so.
        return SourceRead(name="circuit-breakers", present=False)

    findings: list[Finding] = []
    scanned = 0
    for path in sorted(directory.glob("*.jsonl")):
        events = list(_rows(path))
        scanned += len(events)
        if not events:
            continue
        trips = [e for e in events
                 if e.get("event") == "tripped"
                 and _in_window(_parse_ts(e.get("timestamp")), start, end)]
        open_now = events[-1].get("event") == "tripped"
        if not trips and not open_now:
            continue
        times = [t for e in trips if (t := _parse_ts(e.get("timestamp")))]
        first, last = _span(times)
        errors = [str(e.get("error") or "").strip() for e in trips[-MAX_EVIDENCE_LINES:]]
        state = "still open" if open_now else "since reset"
        findings.append(Finding(
            source="circuit-breakers",
            kind="breaker_tripped",
            summary=(
                f"the {path.stem} breaker tripped {len(trips)} time(s) and is {state}"
                if trips else f"the {path.stem} breaker is open from before this window"
            ),
            count=max(len(trips), 1),
            first_at=first,
            last_at=last,
            evidence=tuple(e for e in errors if e),
            # A trip inside the window is an event and earns an evidence
            # report. A breaker still open from before it is a standing
            # condition: worth saying every time, worth filing once. Marking
            # both would write the same report every hour until someone
            # resets it, which is how a report becomes wallpaper.
            defect=bool(trips),
        ))
    return SourceRead(name="circuit-breakers", present=True, scanned=scanned,
                      findings=tuple(findings))


def read_supervisor(start: datetime | None, end: datetime) -> SourceRead:
    """Health-probe failures and the stacks the supervisor dumped over them."""
    from tesseract.paths import log_dir

    directory = log_dir("supervisor")
    if not directory.exists():
        return SourceRead(name="supervisor", present=False)

    incidents = directory / "heartbeat-incidents.jsonl"
    findings: list[Finding] = []
    scanned = 0
    if incidents.exists():
        by_event: dict[str, list[dict[str, Any]]] = {}
        for row in _rows(incidents):
            scanned += 1
            if not _in_window(_parse_ts(row.get("ts")), start, end):
                continue
            by_event.setdefault(str(row.get("event") or "incident"), []).append(row)
        for event, rows in sorted(by_event.items()):
            times = [t for r in rows if (t := _parse_ts(r.get("ts")))]
            first, last = _span(times)
            evidence = [
                f"{r.get('ts')} pid={r.get('backend_pid')} "
                f"failures={r.get('consecutive_failures')} "
                f"{(r.get('last_probe') or {}).get('error', '')}".strip()
                for r in rows[-MAX_EVIDENCE_LINES:]
            ]
            findings.append(Finding(
                source="supervisor",
                kind=event,
                summary=f"the supervisor recorded {len(rows)} × {event.replace('_', ' ')}",
                count=len(rows),
                first_at=first,
                last_at=last,
                evidence=tuple(evidence),
                # A soft failure is the supervisor doing its job; a hard one
                # means it killed and respawned the backend, which is a defect
                # whether or not the restart worked.
                defect="hard" in event or "restart" in event,
            ))

    dumps = [p for p in directory.glob("backend-stack-*.txt")
             if _in_window(_stat_time(p), start, end)]
    if dumps:
        times = sorted(t for p in dumps if (t := _stat_time(p)))
        findings.append(Finding(
            source="supervisor",
            kind="stack_dump",
            summary=f"{len(dumps)} backend stack dump(s) were written",
            count=len(dumps),
            first_at=times[0] if times else None,
            last_at=times[-1] if times else None,
            evidence=tuple(p.name for p in dumps[:MAX_EVIDENCE_LINES]),
            defect=True,
        ))
    return SourceRead(name="supervisor", present=True, scanned=scanned,
                      findings=tuple(findings))


def _stat_time(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


# A log line's own clock. `logsetup` formats with the stdlib default, which is
# LOCAL time carrying no offset, so this is the one stamp in this module that
# must NOT go through `_parse_ts` — that reads a naive value as UTC, which
# would move every backend line into the future by this machine's offset.
# The fraction is captured, not discarded. Floored to the second, a line
# written 0.9 s after the cursor compares equal to the cursor's own second and
# is dropped by `_in_window`'s exclusive lower bound — while the pass that
# wrote that cursor ran before the line existed, so no window ever carries it.
_LINE_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[.,](\d{1,6}))?")
# `<process>-boot-<UTC stamp>-<8 hex>.log`, minted by `bootid.mint_boot_id`.
_BOOT_STAMP = re.compile(r"-boot-(\d{8}T\d{6})-")


def _line_time(line: str) -> datetime | None:
    """The line's own clock, in UTC.

    One hour a year this is an hour out: a naive local stamp inside a DST
    fall-back is ambiguous, `strptime` always yields `fold=0`, and a line from
    the second pass through that hour reads as the first. Accepted rather than
    machined around — the line stays in the log, and the next boot's catch-up
    pass opens a window wide enough to carry it.
    """
    match = _LINE_STAMP.match(line)
    if match is None:
        return None
    try:
        naive = datetime.strptime(match.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if match.group(2):
        naive = naive.replace(microsecond=int(match.group(2).ljust(6, "0")))
    return naive.astimezone(timezone.utc)


def _boot_time(path: Path) -> datetime | None:
    """When the process that owns this file STARTED.

    Its mtime is when the process last wrote, which for a live one is now and
    for a dead one is whenever it died — neither of which is a boot.
    """
    match = _BOOT_STAMP.search(path.name)
    if match is None:
        return _stat_time(path)
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return _stat_time(path)


def read_janitor(start: datetime | None, end: datetime) -> SourceRead:
    """Sweeps that errored. A sweep that cleaned nothing is not a finding."""
    from tesseract.paths import log_dir

    path = log_dir("janitor") / "sweeps.jsonl"
    if not path.exists():
        return SourceRead(name="janitor", present=False)

    scanned = 0
    failed: list[dict[str, Any]] = []
    for row in _rows(path):
        scanned += 1
        if not _in_window(_parse_ts(row.get("finished_at_utc")), start, end):
            continue
        if row.get("errors"):
            failed.append(row)
    if not failed:
        return SourceRead(name="janitor", present=True, scanned=scanned)
    times = [t for r in failed if (t := _parse_ts(r.get("finished_at_utc")))]
    first, last = _span(times)
    evidence = [str(e)[:200] for r in failed for e in (r.get("errors") or [])]
    return SourceRead(
        name="janitor", present=True, scanned=scanned,
        findings=(Finding(
            source="janitor",
            kind="sweep_errors",
            summary=f"{len(failed)} janitor sweep(s) reported errors",
            count=len(failed),
            first_at=first,
            last_at=last,
            evidence=tuple(evidence),
            defect=True,
        ),),
    )


def read_governor(start: datetime | None, end: datetime) -> SourceRead:
    """What autonomy was stopped from doing, and why."""
    from tesseract.paths import log_dir

    path = log_dir("governor") / "pauses.jsonl"
    if not path.exists():
        return SourceRead(name="governor", present=False)

    scanned = 0
    by_reason: Counter[str] = Counter()
    times: dict[str, list[datetime]] = {}
    evidence: dict[str, list[str]] = {}
    for row in _rows(path):
        scanned += 1
        ts = _parse_ts(row.get("ts"))
        if not _in_window(ts, start, end) or row.get("event") != "pause":
            continue
        reason = f"{row.get('source') or '?'}/{row.get('reason') or 'paused'}"
        by_reason[reason] += 1
        times.setdefault(reason, []).append(ts)  # type: ignore[arg-type]
        evidence.setdefault(reason, []).append(
            f"{row.get('ts')} detector={row.get('detector')} "
            f"{json.dumps(row.get('evidence') or {}, sort_keys=True)[:200]}"
        )
    findings = []
    for reason, count in sorted(by_reason.items()):
        first, last = _span(times.get(reason, []))
        findings.append(Finding(
            source="governor",
            kind="paused",
            summary=f"the governor paused {reason} {count} time(s)",
            count=count,
            first_at=first,
            last_at=last,
            evidence=tuple(evidence.get(reason, ())),
        ))
    return SourceRead(name="governor", present=True, scanned=scanned,
                      findings=tuple(findings))


def read_backend(start: datetime | None, end: datetime) -> SourceRead:
    """Errors the backend logged, and how many times it booted.

    Boot count is here rather than in the supervisor's source because it is
    the honest measure of a restart loop: the supervisor only records the
    restarts it caused, and a process that exits on its own leaves nothing
    there but a new log file here.
    """
    from tesseract.paths import log_dir

    directory = log_dir("backend")
    if not directory.exists():
        return SourceRead(name="backend", present=False)

    # An mtime decides only whether a file is worth OPENING: one not written
    # since the window opened cannot hold a line inside it. It used to decide
    # membership outright, and that was wrong in both directions — a live
    # process's log matched every window, so every error in its tail was
    # re-reported hourly for as long as the process ran, while the log being
    # written during this very pass was EXCLUDED, its mtime having moved past
    # `end` before the scan reached it. What belongs in a window is decided by
    # each line's own clock, below.
    logs = [
        p for p in sorted(directory.glob("*.log"))
        if start is None or (t := _stat_time(p)) is None or t > start
    ]
    if not logs:
        return SourceRead(name="backend", present=True)

    classes: Counter[str] = Counter()
    samples: dict[str, str] = {}
    spans: dict[str, list[datetime]] = {}
    for path in logs:
        for line in _tail_lines(path):
            if not _BACKEND_ERROR.search(line):
                continue
            stamped = _line_time(line)
            # A line with no parseable stamp is kept — a continuation line is
            # still evidence and dropping it silently would hide a real error —
            # but it cannot say when anything happened.
            if stamped is not None and not _in_window(stamped, start, end):
                continue
            key = _VOLATILE.sub("#", line.strip())[-160:]
            classes[key] += 1
            if stamped is not None:
                spans.setdefault(key, []).append(stamped)
            # The head of the line, not its tail: what identifies the fault is
            # the logger and the first clause of the message, and a tail begins
            # mid-word in a way no boundary check downstream can repair.
            samples.setdefault(key, line.strip()[:MAX_EVIDENCE_CHARS])

    findings: list[Finding] = []
    # Two processes write per-boot logs into this directory — the backend and
    # the agent controller. Counting the files gave "the backend started 25
    # times" for a window in which it started twenty-one; a report that
    # attributes one process's restarts to another is worse than no count.
    for process, group in sorted(_by_process(logs).items()):
        booted = [p for p in group if _in_window(_boot_time(p), start, end)]
        if len(booted) < 2:
            continue
        group_times = sorted(t for p in booted if (t := _boot_time(p)))
        findings.append(Finding(
            source="backend",
            kind="boots",
            summary=f"{process} started {len(booted)} times in this window",
            count=len(booted),
            first_at=group_times[0] if group_times else None,
            last_at=group_times[-1] if group_times else None,
            evidence=tuple(p.name for p in booted[:MAX_EVIDENCE_LINES]),
            # Two boots is a restart, which happens. The operator's own
            # complaint was a loop, and a loop is what a count makes visible.
            defect=len(booted) >= 4,
        ))
    for key, count in classes.most_common(5):
        first, last = _span(spans.get(key, []))
        findings.append(Finding(
            source="backend",
            kind="logged_error",
            summary=f"{count} log line(s) of: {_readable_summary(samples[key])}",
            count=count,
            first_at=first,
            last_at=last,
            evidence=(samples[key],),
            defect=True,
        ))
    return SourceRead(name="backend", present=True, scanned=len(logs),
                      findings=tuple(findings))


def _by_process(logs: list[Path]) -> dict[str, list[Path]]:
    """`<process>-boot-<stamp>-<id>.log` → the process that wrote it."""
    out: dict[str, list[Path]] = {}
    for path in logs:
        head = path.name.split("-boot-", 1)[0] if "-boot-" in path.name else path.stem
        out.setdefault(head, []).append(path)
    return out


def _tail_lines(path: Path) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = max(0, size - BACKEND_TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    # A tail that began mid-file starts mid-line, and that fragment is not a
    # log line. A file read from the beginning has no such fragment — dropping
    # its first line there loses a real error, which is what it did.
    if start and len(lines) > 1:
        return lines[1:]
    return lines


def read_workers(start: datetime | None, end: datetime) -> SourceRead:
    """Workers that ended badly, grouped by what went wrong.

    Reads the terminal outcome AR-1 made honest: before it, a worker that
    returned no text at all was persisted as `done`, so this source would have
    found eight clean runs where there were eight empty ones.
    """
    from tesseract.orchestrator.workers.heartbeat import STALENESS_THRESHOLD_SECONDS
    from tesseract.orchestrator.workers.record import WorkerStatus, list_active_records

    records = list_active_records()
    by_class: dict[str, list[Any]] = {}
    scanned = 0
    stalled: list[Any] = []
    for record in records:
        if not record.is_terminal():
            # The kernel's own wake already notices these and logs one WARNING
            # per process. What it cannot do is put the fact in front of the
            # operator hours later, which is the reporting half this absorbs —
            # the live alert stays where it is.
            if _is_stale(record):
                stalled.append(record)
            continue
        ts = _parse_ts(getattr(record, "updated_at", None))
        if not _in_window(ts, start, end):
            continue
        scanned += 1
        outcome = record.outcome
        if outcome is not None and outcome in HEALTHY_OUTCOMES:
            continue
        if outcome is None and record.status is WorkerStatus.DONE:
            # A record written before AR-1 carries no outcome. Reading `done`
            # as success is exactly the claim AR-1 removed, so it is left
            # uncounted rather than counted either way.
            continue
        label = record.error_class or (outcome or RunOutcome.FAILED).value
        kind = getattr(record.kind, "value", record.kind)
        by_class.setdefault(f"{kind}/{label}", []).append(record)

    findings = []
    for label, group in sorted(by_class.items()):
        times = [t for r in group if (t := _parse_ts(getattr(r, "updated_at", None)))]
        first, last = _span(times)
        evidence = [
            f"{r.id} status={getattr(r.status, 'value', r.status)} "
            f"outcome={getattr(r.outcome, 'value', '—')} "
            f"{(r.error_message or r.outcome_reason or '')[:200]}".strip()
            for r in group[:MAX_EVIDENCE_LINES]
        ]
        findings.append(Finding(
            source="workers",
            kind="worker_failed",
            summary=f"{len(group)} {label} worker(s) did not complete cleanly",
            count=len(group),
            first_at=first,
            last_at=last,
            evidence=tuple(evidence),
            defect=True,
        ))
    if stalled:
        findings.append(Finding(
            source="workers",
            kind="worker_stalled",
            summary=(
                f"{len(stalled)} worker(s) are still open with no heartbeat for "
                f"over {int(STALENESS_THRESHOLD_SECONDS)}s"
            ),
            count=len(stalled),
            last_at=end,
            evidence=tuple(
                f"{r.id} kind={getattr(r.kind, 'value', r.kind)} "
                f"status={getattr(r.status, 'value', r.status)}"
                for r in stalled[:MAX_EVIDENCE_LINES]
            ),
            # A heartbeat can lag because the loop stalled under a model load,
            # and `workers/liveness.py`'s own docstring is emphatic that this
            # is a true operational fact and a false error. Reported, not filed.
            defect=False,
        ))
    return SourceRead(name="workers", present=True, scanned=scanned,
                      findings=tuple(findings))


def _is_stale(record: Any) -> bool:
    from tesseract.orchestrator.workers.heartbeat import is_heartbeat_stale

    try:
        return is_heartbeat_stale(record.id)
    except OSError:
        return False


def read_conscience(start: datetime | None, end: datetime) -> SourceRead:
    """The drift check's own verdict, as of its last run in the window.

    `conscience_heartbeat` used to publish an escalating transition into the
    agenda queue as its only way of reaching the operator. It is a
    consolidation stage now, and its findings belong in this report instead —
    so the report has to actually carry them, which is what this reads.

    The latest row wins rather than every row: drift is a state, and three
    scrapes of the same bad signal is one problem, not three.
    """
    from tesseract.paths import log_dir

    directory = log_dir("conscience")
    if not directory.exists():
        return SourceRead(name="conscience", present=False)

    latest: dict[str, Any] | None = None
    latest_at: datetime | None = None
    scanned = 0
    for path in sorted(directory.glob("drift-*.jsonl")):
        for row in _rows(path):
            scanned += 1
            ts = _parse_ts(row.get("timestamp"))
            if not _in_window(ts, start, end):
                continue
            if latest_at is None or (ts is not None and ts > latest_at):
                latest, latest_at = row, ts
    if latest is None:
        return SourceRead(name="conscience", present=True, scanned=scanned)

    findings = []
    for signal in latest.get("signals") or []:
        status = str(signal.get("status") or "ok")
        if status == "ok":
            continue
        findings.append(Finding(
            source="conscience",
            kind=f"drift_{status}",
            summary=(
                f"the drift check rates {signal.get('name')} as {status} "
                f"(value {signal.get('value')}, bad at {signal.get('bad')})"
            ),
            last_at=latest_at,
            evidence=(str(signal.get("detail") or "").strip() or "(no detail)",),
            defect=status == "bad",
        ))
    return SourceRead(name="conscience", present=True, scanned=scanned,
                      findings=tuple(findings))


def read_provider_health(start: datetime | None, end: datetime) -> SourceRead:
    """Probe rows that came back wrong, per role."""
    from tesseract.paths import log_dir

    directory = log_dir("provider-health")
    if not directory.exists():
        return SourceRead(name="provider-health", present=False)

    findings: list[Finding] = []
    scanned = 0
    for path in sorted(directory.glob("*.jsonl")):
        drifted: list[dict[str, Any]] = []
        for row in _rows(path):
            scanned += 1
            if not _in_window(_parse_ts(row.get("probed_at")), start, end):
                continue
            if not row.get("ok", True):
                drifted.append(row)
        if not drifted:
            continue
        times = [t for r in drifted if (t := _parse_ts(r.get("probed_at")))]
        first, last = _span(times)
        kinds = Counter(str(r.get("drift_kind") or "unknown") for r in drifted)
        findings.append(Finding(
            source="provider-health",
            kind="provider_drift",
            summary=(
                f"role {path.stem} failed {len(drifted)} probe(s): "
                + ", ".join(f"{k} ×{n}" for k, n in kinds.most_common())
            ),
            count=len(drifted),
            first_at=first,
            last_at=last,
            evidence=tuple(
                f"{r.get('probed_at')} ref={r.get('ref')} "
                f"{json.dumps(r.get('evidence') or {}, sort_keys=True)[:200]}"
                for r in drifted[:MAX_EVIDENCE_LINES]
            ),
            defect=True,
        ))
    return SourceRead(name="provider-health", present=True, scanned=scanned,
                      findings=tuple(findings))


def read_schedule(start: datetime | None, end: datetime) -> SourceRead:
    """Rows that stopped firing, and rows that fired and did not end well.

    The eight sources above read what the runtime WROTE. This one reads what it
    did not: a row that quietly stopped leaves no error line anywhere, and
    every other reader of `runs.jsonl` — the Schedule tab, `schedule_list` —
    renders it rather than reporting on it.
    """
    from tesseract.orchestrator.watchman.rows import read_rows

    report = read_rows(now=end, window_start=start)
    if not report.log_present:
        # Nothing has ever run here. Not the same as every row being late.
        return SourceRead(name="schedule", present=False)

    findings: list[Finding] = []
    for row in report.rows:
        if row.late_by is not None:
            hours = row.late_by.total_seconds() / 3600
            findings.append(Finding(
                source="schedule",
                kind="row_not_firing",
                summary=(
                    f"the {row.name} row has never fired, and it runs "
                    f"{row.fires}"
                    if row.never_ran else
                    f"the {row.name} row runs {row.fires} and is "
                    f"{hours:.1f}h past its next fire"
                ),
                last_at=row.last_run,
                evidence=(
                    f"last run: {row.last_run.isoformat(timespec='seconds')}"
                    if row.last_run else "no run of this row is in the log",
                ),
                defect=True,
            ))
        if not row.unhealthy:
            continue
        counts = Counter(outcome for outcome, _ in row.unhealthy)
        findings.append(Finding(
            source="schedule",
            kind="row_unhealthy",
            summary=(
                f"the {row.name} row ended {len(row.unhealthy)} of "
                f"{row.runs_in_window} run(s) "
                + ", ".join(f"{o} ×{n}" for o, n in counts.most_common())
            ),
            count=len(row.unhealthy),
            last_at=row.last_run,
            evidence=tuple(
                _readable_summary(f"{outcome}: {reason}" if reason else outcome)
                for outcome, reason in row.unhealthy[:MAX_EVIDENCE_LINES]
            ),
            defect=row.defective,
        ))
    return SourceRead(name="schedule", present=True, scanned=report.scanned,
                      findings=tuple(findings))


COLLECTORS: tuple[tuple[str, Callable[[datetime | None, datetime], SourceRead]], ...] = (
    ("circuit-breakers", read_breakers),
    ("supervisor", read_supervisor),
    ("backend", read_backend),
    ("janitor", read_janitor),
    ("governor", read_governor),
    ("workers", read_workers),
    ("provider-health", read_provider_health),
    ("conscience", read_conscience),
    ("schedule", read_schedule),
)


def sweep(*, window_start: datetime | None, window_end: datetime) -> Sweep:
    """Read every source once. Deterministic, no model, no network."""
    reads = tuple(
        _guarded(name, lambda c=collector: c(window_start, window_end))
        for name, collector in COLLECTORS
    )
    return Sweep(window_start=window_start, window_end=window_end, reads=reads)


def default_window_start(end: datetime) -> datetime:
    return end - timedelta(hours=DEFAULT_LOOKBACK_HOURS)


__all__ = [
    "COLLECTORS",
    "DEFAULT_LOOKBACK_HOURS",
    "default_window_start",
    "read_backend",
    "read_breakers",
    "read_conscience",
    "read_governor",
    "read_janitor",
    "read_provider_health",
    "read_schedule",
    "read_supervisor",
    "read_workers",
    "sweep",
]
