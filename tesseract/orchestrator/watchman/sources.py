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

from tesseract.lib.log_envelope import BAD, INFO, SEVERITIES, WARN, parse_ts, read_when
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
# How far under a logged ERROR to look for the exception line that explains it.
# A traceback deeper than this is a stack, not a cause, and the full block is
# in the log file the report points at.
_TRACEBACK_MAX_LINES = 60
# A traceback's own furniture: the header of a nested block, and the two
# sentences Python prints between chained ones. Skipped, not stopped at.
_TRACEBACK_SCAFFOLD = re.compile(
    r"^(Traceback \(most recent call last\)|During handling of the above "
    r"exception|The above exception was the direct cause)"
)
# `ValueError: message`, `pkg.mod.MyError: message`, or a bare `KeyboardInterrupt`.
# A dotted name then a colon or the end of the line, which ordinary prose is
# not: "bare continuation with no timestamp" has a space where the colon goes.
_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*\s*(:|$)")
# The level and logger of a log line, which this tree or a library names. What
# follows is the MESSAGE, which is arbitrary text and stays out of summaries.
_LOG_ORIGIN = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b\s+([\w.]+)")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _many(count: int, one: str) -> str:
    """`1 check`, `3 checks`. Every summary in this file is read by a person,
    in the report and on the Health room's own line, and `check(s)` is a
    plural the reader has to finish."""
    return f"{count} {one}" if count == 1 else f"{count} {one}s"


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


# Every producer in this tree writes ISO 8601 and not one of them agreed on
# the spelling, so this function was the place that knew all of them. It is
# `lib/log_envelope.py` now, because a producer writing the envelope and a
# reader reading it have to agree about the spelling, and two copies of that
# knowledge is how they stop agreeing.
_parse_ts = parse_ts


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


def _recovering(rows: dict[str, dict], name: str) -> bool:
    """Will this breaker try again without anyone doing anything?

    `circuit_breaker.is_recovering` answers it, for everyone who asks. The
    judge and the drift signal ask the same question, and three copies of one
    predicate is three places to correct when it gains a fourth answer.

    Unreadable means recovering: the safe direction is the quiet one, since a
    breaker nobody can read is not evidence that anything is wrong.
    """
    from tesseract.context.circuit_breaker import is_recovering

    row = rows.get(name)
    return True if row is None else is_recovering(row)


def _breaker_rows(directory: Path) -> dict[str, dict]:
    """Every breaker's row, read once. It used to be read per breaker, inside
    the loop below, so a machine with N breakers paid N full directory scans on
    every sweep."""
    try:
        from tesseract.context.circuit_breaker import breaker_report

        return {r["name"]: r for r in breaker_report(directory)}
    except Exception:  # noqa: BLE001
        log.exception("watchman: could not read the breaker report")
        return {}


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
    rows = _breaker_rows(directory)
    for path in sorted(directory.glob("*.jsonl")):
        events = list(_rows(path))
        scanned += len(events)
        if not events:
            continue
        trips = [e for e in events
                 if e.get("event") == "tripped"
                 and _in_window(read_when(e), start, end)]
        open_now = events[-1].get("event") == "tripped"
        if not trips and not open_now:
            continue
        times = [t for e in trips if (t := read_when(e))]
        first, last = _span(times)
        errors = [str(e.get("error") or "").strip() for e in trips[-MAX_EVIDENCE_LINES:]]
        state = "still open" if open_now else "since reset"
        findings.append(Finding(
            source="circuit-breakers",
            kind="breaker_tripped",
            subject=path.stem,
            summary=(
                f"the {path.stem} breaker tripped {_many(len(trips), 'time')} and is {state}"
                if trips else f"the {path.stem} breaker is open from before this window"
            ),
            count=max(len(trips), 1),
            first_at=first,
            last_at=last,
            evidence=tuple(e for e in errors if e),
            # A trip inside the window is an event and earns an evidence
            # report. A breaker still open from before it used to be filed as a
            # standing condition on the reasoning that marking it would write
            # the same report every hour until someone reset it, which is how a
            # report becomes wallpaper.
            #
            # That was true when a trip was permanent. It is not now: a breaker
            # open with a probe due is recovering by itself and IS a condition,
            # and one that will never probe again is a capability that is off
            # until a person acts. Filing the second as an aside is how a
            # 26-hour outage read as a quiet day. The repetition the old
            # reasoning feared is `judge/suppress.py`'s job, and it says this
            # one once.
            severity=BAD if (trips or not _recovering(rows, path.stem)) else INFO,
        ))
    return SourceRead(name="circuit-breakers", present=True, scanned=scanned,
                      findings=tuple(findings))


def _incident_line(row: dict[str, Any]) -> str:
    """One heartbeat incident as a line the operator reads.

    `loop was doing` is the part that matters and the part that was missing:
    without it a heartbeat record says the process went and not what it was
    doing when it went. The BACKEND composes that phrase from its own sampler
    while the loop is blocked, and the supervisor only relays it.
    """
    doing = str(row.get("loop_was_doing") or "").strip()
    parts = (
        f"{row.get('ts')} pid={row.get('backend_pid')}",
        f"failures={row.get('consecutive_failures')}",
        str((row.get("last_probe") or {}).get("error", "")).strip(),
        f"loop was doing: {doing}" if doing else "",
    )
    return " ".join(part for part in parts if part)


def _declared_severity(rows: list[dict[str, Any]]) -> str | None:
    """The worst severity the WRITER put on this group of records.

    `None` where no row declared one, which is what a file written before the
    envelope existed looks like. Reading the writer's word rather than
    re-deciding from the record's name is the rule everywhere else here; the
    caller keeps its own fallback for exactly the rows this cannot answer for.
    """
    declared = {str(r.get("severity")) for r in rows} & set(SEVERITIES)
    if not declared:
        return None
    return BAD if BAD in declared else (WARN if WARN in declared else INFO)


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
            if not _in_window(read_when(row), start, end):
                continue
            by_event.setdefault(str(row.get("event") or "incident"), []).append(row)
        for event, rows in sorted(by_event.items()):
            times = [t for r in rows if (t := read_when(r))]
            first, last = _span(times)
            evidence = [_incident_line(r) for r in rows[-MAX_EVIDENCE_LINES:]]
            findings.append(Finding(
                source="supervisor",
                kind=event,
                subject="supervisor",
                summary=f"the supervisor recorded {len(rows)} × {event.replace('_', ' ')}",
                count=len(rows),
                first_at=first,
                last_at=last,
                evidence=tuple(evidence),
                # The writer's own word where it declared one. A soft failure is
                # the supervisor doing its job; a hard one means it killed and
                # respawned the backend, which is a defect whether or not the
                # restart worked. The name rule is the fallback, for rows
                # written before these records carried a severity at all.
                severity=_declared_severity(rows) or (
                    BAD if ("hard" in event or "restart" in event) else WARN
                ),
            ))

    dumps = [p for p in directory.glob("backend-stack-*.txt")
             if _in_window(_stat_time(p), start, end)]
    if dumps:
        times = sorted(t for p in dumps if (t := _stat_time(p)))
        findings.append(Finding(
            source="supervisor",
            kind="stack_dump",
            subject="backend",
            summary=f"{_many(len(dumps), 'backend stack dump')} were written",
            count=len(dumps),
            first_at=times[0] if times else None,
            last_at=times[-1] if times else None,
            evidence=tuple(p.name for p in dumps[:MAX_EVIDENCE_LINES]),
            severity=BAD,
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
        if not _in_window(read_when(row), start, end):
            continue
        if row.get("errors"):
            failed.append(row)
    if not failed:
        return SourceRead(name="janitor", present=True, scanned=scanned)
    times = [t for r in failed if (t := read_when(r))]
    first, last = _span(times)
    evidence = [str(e)[:200] for r in failed for e in (r.get("errors") or [])]
    return SourceRead(
        name="janitor", present=True, scanned=scanned,
        findings=(Finding(
            source="janitor",
            kind="sweep_errors",
            subject="janitor",
            summary=f"{_many(len(failed), 'janitor sweep')} reported errors",
            count=len(failed),
            first_at=first,
            last_at=last,
            evidence=tuple(evidence),
            severity=BAD,
        ),),
    )


def read_loop_stalls(start: datetime | None, end: datetime) -> SourceRead:
    """How long the app was blocked for, and what it was in while it was.

    One finding for the window rather than one per stall: a machine under load
    produces a run of them and they are one condition, not twenty. The
    severity follows the worst one, because a two second pause and a
    twelve second block are not the same news even though the same sampler
    wrote both.
    """
    from tesseract.orchestrator import loop_stalls

    directory = loop_stalls.root()
    if not directory.is_dir():
        return SourceRead(name="loop-stalls", present=False)

    rows = loop_stalls.read_since(start, end)
    if not rows:
        return SourceRead(name="loop-stalls", present=True)
    seconds = [float(r.get("seconds") or 0.0) for r in rows]
    worst = max(seconds)
    times = [t for r in rows if (t := read_when(r))]
    first, last = _span(times)
    return SourceRead(
        name="loop-stalls", present=True, scanned=len(rows),
        findings=(Finding(
            source="loop-stalls",
            kind=loop_stalls.KIND,
            subject="event loop",
            summary=(
                f"the app was blocked {_many(len(rows), 'time')}, "
                f"the longest for {worst:.1f} seconds"
            ),
            count=len(rows),
            first_at=first,
            last_at=last,
            # The frames, worst first, so the line the operator reads names
            # what held the thread rather than only how long it was held.
            evidence=tuple(
                f"{float(r.get('seconds') or 0.0):.1f}s  {r.get('doing') or ''}".strip()
                for r in sorted(
                    rows, key=lambda r: float(r.get("seconds") or 0.0), reverse=True,
                )[:MAX_EVIDENCE_LINES]
            ),
            # The writer already graded each row against the point where a
            # block costs a heartbeat. Read the word rather than deciding
            # again from the number.
            severity=BAD if any(
                r.get("severity") == BAD for r in rows
            ) else WARN,
        ),),
    )


def read_interpreter(start: datetime | None, end: datetime) -> SourceRead:
    """Which Python is running this, and whether it is the one that should be.

    An install has one app and one interpreter beside it, so there is nothing
    to get wrong. A dev checkout has two: the `.venv` everything is installed
    into, and whatever Python happens to be on PATH. `supervisor/daemon.py`
    spawns with `sys.executable`, so whichever one started the supervisor
    decides what the whole runtime imports.

    On 2026-08-18 that crossed between them with no code change and voice was
    gone for a day. Both work today because `.venv` is reconciled, and the
    thing that was never fixed is the silence: a runtime on the wrong
    interpreter boots fine and quietly lacks whatever that interpreter is
    missing, and no source could say so because nothing looked.

    A collector, not a guard. The operator declined a boot guard, and refusing
    to start is a different act from saying which one is running.

    **It reports a STATE, not something that happened in a window**, which is
    why `start` goes unread while every other collector here uses it. Which
    interpreter is running has no beginning to fall inside a sweep: it is true
    now or it is not, so the finding recurs every tick until it is fixed and
    the judge's suppress stage is what stops it being said twice.
    """
    import sys

    from tesseract.paths import is_installed_tree

    running = Path(sys.executable)
    if is_installed_tree():
        # One app, one interpreter, nothing to compare against. Reported as
        # present and quiet rather than skipped, so the panel can tell an
        # install apart from a source that failed to look.
        return SourceRead(name="interpreter", present=True, scanned=1)

    expected = _venv_python()
    if expected is None:
        return SourceRead(name="interpreter", present=True, scanned=1)
    if _same_file(running, expected):
        return SourceRead(name="interpreter", present=True, scanned=1)
    return SourceRead(
        name="interpreter", present=True, scanned=1,
        findings=(Finding(
            source="interpreter",
            kind="wrong_interpreter",
            subject="the interpreter",
            summary=(
                "this checkout is running a Python that is not its own .venv, "
                "so what it can import is whatever that one has"
            ),
            count=1,
            last_at=end,
            evidence=(f"running: {running}", f"expected: {expected}"),
            severity=BAD,
        ),),
    )


def _venv_python() -> Path | None:
    """The interpreter this checkout is meant to run, or None if it has none."""
    from tesseract.paths import TESSERACT_DIR

    for relative in ("Scripts/python.exe", "bin/python"):
        candidate = TESSERACT_DIR.parent / ".venv" / relative
        if candidate.exists():
            return candidate
    return None


def _same_file(one: Path, two: Path) -> bool:
    """Whether two paths are the same interpreter, symlinks and case included.

    `samefile` is the real answer and it raises when either side is gone, so
    the resolved-path comparison is the fallback rather than the rule.
    """
    try:
        return one.samefile(two)
    except OSError:
        return one.resolve() == two.resolve()


def read_machine(start: datetime | None, end: datetime) -> SourceRead:
    """Whether the computer was there, from the computer's own record.

    Two findings and they answer different questions. A stretch the machine
    was ASLEEP or shut down is `info`: it is the runtime describing why it was
    quiet, and it is what stops every silence in the window reading as a fault.
    A stretch it LOST POWER is `bad`, because that is the one the operator can
    act on and the one nothing else on this machine reports at all.

    This is the source that lets a report say the app broke rather than
    implying it. Before it, a night asleep and a runtime that died looked the
    same from every log the watchman reads.

    **It reads wider than the sweep and reports narrower.** A gap is only known
    once both its ends are in what was read, so an absence that began last
    night and ended in this morning's window needs the read to reach back past
    the window. The consequence, which no other collector here has: a finding's
    `first_at` can predate the sweep's own start, and that is the real
    beginning rather than a bug.
    """
    from tesseract.orchestrator import machine_state

    # Read WIDER than the sweep, then filter to it. `gaps` opens no gap for a
    # wake whose sleep is not in what it read, on purpose, so reading from the
    # sweep's own start loses exactly the absence a morning report is written
    # inside: the machine went to sleep last night, before the window, and woke
    # in it. `judge/attribute.py` already reads a week for this reason.
    since = min(start or end, end - machine_state.LOOKBACK)
    found = machine_state.gaps(since, now=end)
    if found is None:
        # NOT `present=False`. The power record exists on every Windows
        # machine; failing to read it is a permissions refusal, a corrupted
        # log, or a stopped Event Log service. `present=False` renders as
        # "nothing on this machine writes this yet", which would be this
        # source telling the operator the opposite of what happened, in the
        # one room built to tell a thing that is not there from a thing that
        # could not be read.
        return SourceRead(
            name="machine", present=True,
            error="the machine's own power record could not be read",
        )
    # Filtered on the sweep's own window, and `start` is used rather than
    # `since`: what was READ is deliberately wider, what is REPORTED is what
    # the sweep asked about.
    inside = [g for g in found if _in_window(g.ended, start, end)]
    if not inside:
        return SourceRead(name="machine", present=True)

    findings: list[Finding] = []
    for kind, severity in ((machine_state.LOST_POWER, BAD), (machine_state.ASLEEP, INFO),
                           (machine_state.SHUT_DOWN, INFO)):
        of_kind = [g for g in inside if g.kind == kind]
        if not of_kind:
            continue
        longest = max(g.seconds for g in of_kind)
        findings.append(Finding(
            source="machine",
            kind=f"machine_{kind.replace(' ', '_')}",
            subject="the machine",
            summary=(
                f"the machine was {kind} {_many(len(of_kind), 'time')}, "
                f"the longest for {longest / 3600:.1f} hours"
                if longest >= 3600 else
                f"the machine was {kind} {_many(len(of_kind), 'time')}, "
                f"the longest for {longest / 60:.0f} minutes"
            ),
            count=len(of_kind),
            first_at=min(g.began for g in of_kind),
            last_at=max(g.ended for g in of_kind),
            evidence=tuple(g.said() for g in of_kind[-MAX_EVIDENCE_LINES:]),
            severity=severity,
        ))
    return SourceRead(name="machine", present=True, scanned=len(found),
                      findings=tuple(findings))


def read_repairs(attempts: "Iterable[Any]") -> SourceRead:
    """What the runtime put right about itself on this pass, and what it could
    not.

    Takes the attempts rather than reading a file, and that is deliberate: the
    repair pass runs inside this same tick, so its outcome is a fact about NOW
    rather than about the window. Every other source here reads a record
    because the thing it describes happened while nobody was looking.

    Three severities, and the split is the batch:

    - a repair that FAILED or has given up is something broken that the
      runtime cannot fix, which is what `bad` means and what the needs-you
      path is for;
    - a repair that WORKED is `info`. It is news. Sending "I fixed it" down
      the needs-you path is how a channel gets muted, and muting it costs the
      messages that matter;
    - a check that could not RUN is `warn`. Not knowing whether a thing is
      broken is a different answer from it being broken, and dressing the
      first as the second sends a person after a fault that may not exist.

    Nothing is emitted for `nothing to do`, which is almost every pass.
    """
    from tesseract.orchestrator.repairs import BREAKER_PREFIX, REPAIRS

    declared = {repair.key: repair for repair in REPAIRS}
    findings: list[Finding] = []
    scanned = 0
    for attempt in attempts:
        scanned += 1
        outcome = getattr(attempt, "outcome", "")
        if outcome == "nothing to do":
            continue
        key = getattr(attempt, "key", "")
        said = str(getattr(attempt, "said", ""))
        row = declared.get(key)
        broke = row.what_broke if row is not None else key
        title = row.title if row is not None else key

        if outcome == "repaired":
            severity = INFO
            summary = f"the runtime fixed it: {broke}"
        elif outcome == "held":
            severity = BAD
            summary = (
                f"{broke}, and the runtime has stopped trying to fix it. "
                f"Start it again with breaker_reset {BREAKER_PREFIX}{key} once "
                f"the cause is gone"
            )
        elif outcome == "could not tell":
            severity = WARN
            summary = f"the runtime could not tell whether this is still true: {broke}"
        else:
            severity = BAD
            summary = f"{broke}, and the runtime tried to fix it and could not"

        findings.append(Finding(
            source="repairs",
            kind=f"repair_{outcome.replace(' ', '_')}",
            # The repair's own key, so the judge matches the same thing tick
            # to tick rather than parsing it back out of a sentence.
            subject=key,
            summary=summary,
            cause=said,
            severity=severity,
            # `said` on a failure is `f"{type(exc).__name__}: {exc}"`, which is
            # text this runtime was HANDED. It reaches the operator's own files
            # in the evidence and never the narration model. What the model
            # gets is the declaration, which the runtime wrote.
            quotable=False,
            model_summary=(
                f"{title}: {outcome}" if outcome != "repaired"
                else f"{title}: done"
            ),
            evidence=(said,) if said else (),
        ))
    return SourceRead(name="repairs", present=True, scanned=scanned,
                      findings=tuple(findings))


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
        ts = read_when(row)
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
            subject=reason,
            summary=f"the governor paused {reason} {_many(count, 'time')}",
            # The governor doing its job is not a fault.
            severity=INFO,
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
    # since the window opened cannot hold a line inside it. Deciding
    # membership by mtime is wrong in both directions: a live process's log
    # matches every window, so every error in its tail is re-reported hourly
    # for as long as the process runs, while the log written during this very
    # pass is EXCLUDED, its mtime having moved past `end` before the scan
    # reached it. What belongs in a window is decided by each line's own
    # clock, below.
    logs = [
        p for p in sorted(directory.glob("*.log"))
        if start is None or (t := _stat_time(p)) is None or t > start
    ]
    if not logs:
        return SourceRead(name="backend", present=True)

    classes: Counter[str] = Counter()
    samples: dict[str, str] = {}
    causes: dict[str, str] = {}
    spans: dict[str, list[datetime]] = {}
    # Which per-boot files an error class appears in. A line's own stamp
    # cannot answer "did the current process hit this": the stamp is a local
    # wall clock and the boot time is a file mtime, so comparing them turns
    # every line in a file into a line from before that file's own boot on any
    # machine whose offset is not zero. The FILE is the process. If a class
    # appears in the newest file of the process that logged it, that process
    # has hit it; if it does not, the process that did has been replaced.
    seen_in: dict[str, set[Path]] = {}
    for path in logs:
        tail = _tail_lines(path)
        for index, line in enumerate(tail):
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
            seen_in.setdefault(key, set()).add(path)
            if cause := _exception_line(tail, index):
                causes.setdefault(key, cause)

    findings: list[Finding] = []
    # The newest per-boot file of each process. An error that appears in none
    # of them was logged by a process that has since been replaced, and saying
    # so is the difference between a report and a wrong instruction: a sweep
    # whose window spanned a restart reported `permissions.yaml lists 1
    # unregistered tool(s) — prune them: autonomy_read` as a live fault, and
    # the assistant reading it told the operator to delete a tool that had been
    # registered for an hour. The breaker finding below already draws this
    # distinction for its own window; a logged error is the one that most needs
    # it, because a restart is exactly what fixes most of them.
    live_logs = {
        max(group, key=lambda p: _boot_time(p) or datetime.min.replace(tzinfo=timezone.utc))
        for group in _by_process(logs).values()
        if group
    }

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
            subject=process,
            summary=f"{process} started {len(booted)} times in this window",
            count=len(booted),
            first_at=group_times[0] if group_times else None,
            last_at=group_times[-1] if group_times else None,
            evidence=tuple(p.name for p in booted[:MAX_EVIDENCE_LINES]),
            # Two boots is a restart, which happens. The operator's own
            # complaint was a loop, and a loop is what a count makes visible.
            # Two boots is a restart, which happens. Four in one window is a
            # loop, and a loop is worth a person's eyes without being damage
            # in itself: the fault that causes it is reported separately.
            severity=WARN if len(booted) >= 4 else INFO,
        ))
    for key, count in classes.most_common(5):
        first, last = _span(spans.get(key, []))
        cause = causes.get(key, "")
        # This class appears in no process's current log, so nothing running
        # now has hit it. Not dropped: it happened, and a fault that a restart
        # only hides comes back. Labelled, so a reader knows it is history
        # rather than a thing to go and fix.
        before_boot = bool(live_logs) and not (seen_in.get(key, set()) & live_logs)
        since = " (before the last restart, and not seen since)" if before_boot else ""
        findings.append(Finding(
            source="backend",
            kind="logged_error",
            subject=_log_origin(samples[key]),
            summary=f"{_many(count, 'log line')} of: {_readable_summary(samples[key])}{since}",
            # What the MODEL is given instead. The operator keeps the whole
            # sentence: a truncated one reached this operator's phone once,
            # and undoing that to close a disclosure path would trade a real
            # delivered thing for a hypothetical.
            # But `fact_lines` emits a summary for every finding regardless of
            # `quotable`, so a raw log line in it is the gate's last open
            # door. Two renderings of one finding settles both: the level and
            # the logger are chosen by whoever wrote the module, the message
            # is whatever the runtime was handed.
            model_summary=(
                f"{_many(count, 'error line')} logged by "
                f"{_log_logger(samples[key])}{since}"
            ),
            # The traceback's last line, which four unrelated-looking loggers
            # can share. It is what lets the reader say "one problem" where it
            # used to say four.
            cause=cause,
            count=count,
            first_at=first,
            last_at=last,
            evidence=(samples[key],) + ((cause,) if cause else ()),
            # An error the current process has never hit is not this process's
            # problem. It stays in the report, one rung down, because "it
            # stopped when we restarted" is a thing worth seeing and not a
            # thing worth being paged about.
            severity=WARN if before_boot else BAD,
        ))
    return SourceRead(name="backend", present=True, scanned=len(logs),
                      findings=tuple(findings))


def _log_origin(line: str) -> str:
    """`ERROR from tesseract.mirror.server.config_watcher`, or just the level.

    Names where a log line came from without quoting what it said. A logger
    name is chosen by whoever wrote the module; a message can contain
    anything the runtime was handed.
    """
    match = _LOG_ORIGIN.search(_LEADING_STAMP.sub("", line))
    if match is None:
        return "unattributed error"
    return f"{match.group(1)} from {match.group(2)}"


def _log_logger(line: str) -> str:
    """The logger alone, for a sentence that has already said `error`.

    `_log_origin` carries the level because the subject it names has to stand
    on its own. A summary beside that subject repeating it reads as
    `1 ERROR from x.y line(s)`, which is the shape the operator saw on the one
    line the Health room opens with.
    """
    match = _LOG_ORIGIN.search(_LEADING_STAMP.sub("", line))
    return match.group(2) if match else "something that named no logger"


def _exception_line(lines: list[str], index: int) -> str:
    """The `SomeError: what went wrong` line under a logged ERROR, if there is one.

    An `ERROR` line names where a fault surfaced; the sentence a person needs
    is at the BOTTOM of the traceback under it, and until now nothing read that
    far. `config_watcher: mirror.yaml refresh failed` reached the operator
    while `RuntimeError: mirror.yaml missing required 'identity.name'` sat four
    lines below it in the same file.

    **The LAST exception, not the first.** A chained traceback prints the
    superseded cause first and the one that actually ended the operation last,
    so taking the first match reported `ValueError` for something a
    `RuntimeError` killed. Every `raise ... from ...` has this shape.

    **And only lines SHAPED like an exception.** Two earlier attempts at that
    were wrong in opposite directions. Taking any flush-left line let a bare
    untimestamped continuation line overwrite the real exception. Skipping
    lines containing "exception occurred" dropped a real
    `RuntimeError: exception occurred during startup`, because that phrase is
    ordinary English as well as a chain marker. What separates them is not
    the words but the form: an exception line is a dotted name, then a colon.
    Scaffolding is skipped, anything else ends the block.
    """
    if index + 1 >= len(lines) or "Traceback (most recent call last)" not in lines[index + 1]:
        return ""
    found = ""
    for line in lines[index + 2:index + 2 + _TRACEBACK_MAX_LINES]:
        if not line.strip() or line[:1].isspace():
            continue
        if _LEADING_STAMP.match(line):
            break
        stripped = line.strip()
        if _TRACEBACK_SCAFFOLD.match(stripped):
            continue
        if not _EXCEPTION_LINE.match(stripped):
            break
        found = stripped[:MAX_EVIDENCE_CHARS]
    return found


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

    Reads the terminal outcome rather than the status. A worker that returns
    no text at all persisted as `done` shows this source eight clean runs
    where there were eight empty ones.
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
            # An older record carries no outcome, and reading `done` as
            # success is the claim this source exists to stop making, so it
            # is left uncounted rather than counted either way.
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
            subject=label,
            summary=f"{_many(len(group), f'{label} worker')} did not complete cleanly",
            count=len(group),
            first_at=first,
            last_at=last,
            evidence=tuple(evidence),
            severity=BAD,
        ))
    if stalled:
        findings.append(Finding(
            source="workers",
            kind="worker_stalled",
            # Not a class name: this finding is about the open set, not about
            # one error class, and the loop above that binds `label` does not
            # necessarily run.
            subject="open workers",
            summary=(
                f"{_many(len(stalled), 'worker')} still open with no heartbeat for "
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
            severity=WARN,
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

    `conscience_heartbeat` is a consolidation stage, not a publisher of
    escalating transitions into the agenda queue. Its findings belong in this
    report, so the report has to carry them, which is what this reads.

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
            ts = read_when(row)
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
            subject=str(signal.get("name") or ""),
            summary=(
                f"the drift check rates {signal.get('name')} as {status} "
                f"(value {signal.get('value')}, bad at {signal.get('bad')})"
            ),
            last_at=latest_at,
            evidence=(str(signal.get("detail") or "").strip() or "(no detail)",),
            # The conscience already grades itself, in the same three words.
            severity=BAD if status == "bad" else WARN,
        ))
    return SourceRead(name="conscience", present=True, scanned=scanned,
                      findings=tuple(findings))


def read_provider_health(start: datetime | None, end: datetime) -> SourceRead:
    """Each ref's CONDITION as of its newest row in the window.

    `read_conscience` above takes only the latest row because drift is a
    state; a provider is the same kind of thing. Quota resets and servers come
    back, so a ref that failed at 23:01 and answered at 23:40 is not a defect
    — reporting it as one sends the operator after a fault that has already
    passed. The failures behind it stay as the count and the evidence, which
    is what says whether this was a blip or an hour of them.
    """
    from tesseract.paths import log_dir

    directory = log_dir("provider-health")
    if not directory.exists():
        return SourceRead(name="provider-health", present=False)

    findings: list[Finding] = []
    scanned = 0
    for path in sorted(directory.glob("*.jsonl")):
        in_window: list[tuple[datetime | None, dict[str, Any]]] = []
        for row in _rows(path):
            scanned += 1
            ts = read_when(row)
            if not _in_window(ts, start, end):
                continue
            in_window.append((ts, row))
        if not in_window:
            continue
        # File order is append order, so an unstamped row keeps its position
        # rather than sorting to the front and speaking for the ref.
        newest_ts, newest = in_window[-1]
        for ts, row in in_window:
            if ts is not None and (newest_ts is None or ts > newest_ts):
                newest_ts, newest = ts, row
        if newest.get("ok", True):
            continue
        drifted = [r for _ts, r in in_window if not r.get("ok", True)]
        times = [t for t, _r in in_window if t is not None and not _r.get("ok", True)]
        first, _last = _span(times)
        kinds = Counter(str(r.get("drift_kind") or "unknown") for r in drifted)
        extra = newest.get("extra") or {}
        roles = [str(r) for r in (extra.get("roles") or [])]
        # What a role reaches for FIRST, which is a different list from the
        # roles that merely name it. A ref nothing leads with is a spare, and
        # a spare failing has cost nothing yet: every role that names it is
        # still being answered by the entry above it. Grading the two the same
        # is what made one slow answer from a fallback read as an emergency
        # (operator, 2026-09-02: "if a 2nd fallback failed, and it is not yet
        # triggered, no need to create major alarm yet").
        leads = [str(r) for r in (extra.get("leads") or [])]
        wearing = (
            f" (what {', '.join(leads)} reaches for first)" if leads
            else f" (a spare for {', '.join(roles)})" if roles
            else ""
        )
        findings.append(Finding(
            source="provider-health",
            kind="provider_drift",
            subject=path.stem,
            summary=(
                f"{path.stem}{wearing} is failing as of its last check: "
                + ", ".join(f"{k} ×{n}" for k, n in kinds.most_common())
                + f" across {len(drifted)} of {_many(len(in_window), 'check')}"
            ),
            count=len(drifted),
            first_at=first,
            last_at=newest_ts,
            evidence=tuple(
                f"{r.get('probed_at')} ref={r.get('ref')} "
                f"{json.dumps(r.get('evidence') or {}, sort_keys=True)[:200]}"
                for r in drifted[-MAX_EVIDENCE_LINES:]
            ),
            # **Position decides this, not the fact of a failure.** The line
            # this replaces read `BAD if kinds else WARN`, and `kinds` cannot
            # be empty here: we only reach this point when the newest check
            # failed, so at least one row is in `drifted` and the WARN branch
            # had never once been taken. Every drift was an emergency,
            # including one slow answer from a ref nothing leads with.
            #
            # `INFO` and not `WARN`, because of where each one lands: the
            # panel bands `warn` into *needs action* beside `bad`, so grading
            # a spare `warn` would have moved it from red to amber inside the
            # same band and changed nothing about the alarm. `info` puts it in
            # Operating, where it is still a line with its own sentence and
            # still says what happened. Visible, not demanding.
            #
            # A ref with no `leads` list is one probed before this was
            # recorded, and it reads as a spare. That is the safe direction
            # for an unknown here: a row that cannot say whether anything
            # leads with it cannot claim a degradation either.
            #
            # What is deliberately NOT read: whether a role has already fallen
            # through to this spare. That would be the sharper rule and it is
            # not one finding's to answer, because each ref is its own row and
            # nothing here can see the state of the entry above it.
            severity=BAD if leads else INFO,
        ))
    return SourceRead(name="provider-health", present=True, scanned=scanned,
                      findings=tuple(findings))


def read_schedule(start: datetime | None, end: datetime) -> SourceRead:
    """Rows that stopped firing, and rows that fired and did not end well.

    The eight sources above read what the runtime WROTE. This one reads what it
    did not: a row that quietly stopped leaves no error line anywhere, and
    every other reader of `runs.jsonl` — Managed system, `schedule_list` —
    renders it rather than reporting on it.
    """
    from tesseract.orchestrator.watchman.rows import read_rows

    report = read_rows(now=end, window_start=start)
    if not report.log_present:
        # Nothing has ever run here. Not the same as every row being late.
        return SourceRead(name="schedule", present=False)

    findings: list[Finding] = []
    # One line for the outage, before the rows. Not a defect: a machine that
    # slept did nothing wrong, and reporting it as one is what produced
    # "the watchman row is 11.0h past its next fire" for eleven hours in which
    # every row on the machine was equally silent.
    for began, ended in report.stalls:
        hours = (ended - began).total_seconds() / 3600
        findings.append(Finding(
            source="schedule",
            kind="runtime_stalled",
            subject="",
            summary=(
                f"nothing was scheduled to run for {hours:.1f}h. The runtime "
                f"was asleep, stopped, or stalled, and no row is late for it"
            ),
            first_at=began,
            last_at=ended,
            evidence=(
                f"last fire before: {began.isoformat(timespec='seconds')}",
                f"first fire after: {ended.isoformat(timespec='seconds')}",
            ),
            # The machine slept. Nothing broke and nothing is late for it.
            severity=INFO,
            quotable=True,
        ))
    for row in report.rows:
        if row.late_by is not None:
            hours = row.late_by.total_seconds() / 3600
            findings.append(Finding(
                source="schedule",
                kind="row_not_firing",
                subject=row.name,
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
                severity=BAD,
            ))
        if not row.unhealthy:
            continue
        counts = Counter(outcome for outcome, _ in row.unhealthy)
        findings.append(Finding(
            source="schedule",
            kind="row_unhealthy",
            subject=row.name,
            summary=(
                f"the {row.name} row ended {len(row.unhealthy)} of "
                f"{_many(row.runs_in_window, 'run')} "
                + ", ".join(f"{o} ×{n}" for o, n in counts.most_common())
            ),
            count=len(row.unhealthy),
            last_at=row.last_run,
            evidence=tuple(
                _readable_summary(f"{outcome}: {reason}" if reason else outcome)
                for outcome, reason in row.unhealthy[:MAX_EVIDENCE_LINES]
            ),
            severity=BAD if row.defective else WARN,
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
    ("loop-stalls", read_loop_stalls),
    ("machine", read_machine),
    ("interpreter", read_interpreter),
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
    "read_interpreter",
    "read_janitor",
    "read_loop_stalls",
    "read_machine",
    "read_provider_health",
    "read_repairs",
    "read_schedule",
    "read_supervisor",
    "read_workers",
    "sweep",
]
