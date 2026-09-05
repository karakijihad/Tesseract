"""Stage 2 — is it still true NOW?

**Windowed truth is not truth.** The window says what happened between two
timestamps; the operator needs to know what is wrong when they read the
message. A collector cannot answer that, because it is looking at the window
by definition. So this stage looks FORWARD from each finding's last
occurrence, at the same files, and asks whether the thing has since stopped
being the case.

One rule per kind of evidence that can settle it. Every one of them is a
lookup at the newest thing on disk, later than the finding's last occurrence:

- **A row that has since run cleanly is not failing.** `runs.jsonl` already
  carries every row's latest outcome across the whole log, not just the
  window, so this is a lookup rather than a second scan.
- **A row that has since fired is not late.** Same record, same lookup.
- **A provider that answered on its next probe is not failing.** AR-12 already
  made provider health a STATE keyed by ref; this stage is where a later `ok`
  row actually clears an earlier failure.
- **A breaker that reset is closed.** The breaker log records the reset; the
  collector reports the trips inside the window either way.
- **A drift signal the check has since rated `ok` is not drifting.** The
  conscience log is the same shape as provider health: a state, re-stated
  every run, and the collector already takes only the latest row IN the
  window. This takes the latest row full stop.
- **A source the governor is no longer pausing is not paused.** The pause is
  the only one of these that keeps its state somewhere other than its log:
  `source-pauses.json` is the live set, so the question is answered by
  absence from it rather than by finding an `unpause` row.
- **A worker kind that has completed cleanly since is not failing.** Grouped
  by `<kind>/<error class>`, so the question is about the kind: a later
  terminal record of that kind with a healthy outcome says the class cleared.
- **Workers that are no longer open are no longer stalled.** The finding
  names the open set; if nothing is open and stale now, there is nothing to
  report.
- **A janitor sweep that has since finished without errors clears the
  earlier ones.** Same lookup, on `sweeps.jsonl`.

**Where no rule exists, the finding survives.** That is the safe direction and
it is stated rather than implied: a judge that drops what it cannot evaluate
is the silent filter this phase exists to prevent. Adding a kind here means
adding evidence that settles it, not adding a default.

**What is deliberately left unresolved.** `boots` and `runtime_stalled` are
CONDITIONS, not faults — a restart count and an outage window are facts about
the window that no later evidence makes untrue, and stages 3 and 5 are what
give them their place in the report. `logged_error` and `stack_dump` are the
real remaining gap: nothing on disk says the operation a log line named later
succeeded. The `brief_delivery: sent <date>` marker is the shape of the
answer, and there is no general version of it yet.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tesseract.orchestrator.watchman.judge import Verdict

log = logging.getLogger(__name__)

STAGE = "resolve"

# The outcomes that mean a scheduled run ended the way it was meant to.
# Imported rather than restated so this cannot drift from the scheduler's own
# vocabulary.
from tesseract.orchestrator.outcome import HEALTHY_OUTCOMES  # noqa: E402


def apply(verdicts: list["Verdict"], *, now: datetime) -> list["Verdict"]:
    """Return the verdicts with anything since settled marked `dropped`."""
    from tesseract.orchestrator.watchman.judge import DROPPED, Verdict

    rows = _row_states(now)
    out: list[Verdict] = []
    for verdict in verdicts:
        if not verdict.kept:
            out.append(verdict)
            continue
        why = _settled(verdict.finding, rows=rows, now=now)
        out.append(
            Verdict(finding=verdict.finding, stage=STAGE, action=DROPPED, why=why)
            if why
            else verdict
        )
    return out


def _settled(finding, *, rows: dict, now: datetime) -> str:
    """Why this finding is no longer true, or `""` when it still is."""
    if finding.kind == "row_unhealthy":
        return _row_recovered(finding, rows)
    if finding.kind == "row_not_firing":
        return _row_fired_since(finding, rows)
    if finding.kind == "provider_drift":
        return _provider_answered_since(finding)
    if finding.kind == "breaker_tripped":
        return _breaker_reset(finding)
    if finding.kind.startswith("drift_"):
        return _drift_rated_ok_since(finding)
    if finding.kind == "paused":
        return _pause_lifted(finding)
    if finding.kind == "worker_failed":
        return _worker_kind_completed_since(finding)
    if finding.kind == "worker_stalled":
        return _nothing_is_stalled_now(finding)
    if finding.kind == "sweep_errors":
        return _sweep_clean_since(finding)
    return ""


def _row_states(now: datetime) -> dict:
    """Every row's latest run, across the whole log rather than the window.

    One read for the whole stage. `read_rows` with no window start is the
    reader that owns this file's format, and its `RowState` already carries
    the two fields every rule below needs.
    """
    try:
        from tesseract.orchestrator.watchman.rows import read_rows

        report = read_rows(now=now, window_start=None)
    except Exception:  # noqa: BLE001 — a stage that cannot read resolves nothing
        log.warning("judge: the run log could not be read; resolving no rows", exc_info=True)
        return {}
    return {row.name: row for row in report.rows}


def _row_recovered(finding, rows: dict) -> str:
    row = rows.get(finding.subject)
    if row is None or row.last_run is None or finding.last_at is None:
        return ""
    if row.last_run <= finding.last_at:
        return ""
    if row.last_outcome not in HEALTHY_OUTCOMES:
        return ""
    return (
        f"{finding.subject} has run cleanly since "
        f"({row.last_outcome} at {row.last_run.isoformat(timespec='seconds')})"
    )


def _row_fired_since(finding, rows: dict) -> str:
    row = rows.get(finding.subject)
    if row is None or row.late_by is not None or row.never_ran:
        return ""
    when = row.last_run.isoformat(timespec="seconds") if row.last_run else "since"
    return f"{finding.subject} has fired since and is on time now (last at {when})"


def _provider_answered_since(finding) -> str:
    """A ref whose newest row on disk says ok, later than the finding.

    The collector already takes the newest row IN THE WINDOW; this reads the
    newest row full stop, which is what makes a probe that ran after the
    window closed able to clear a failure inside it.
    """
    from tesseract.orchestrator.provider_health import tail_recent

    try:
        rows = tail_recent(finding.subject, n=8)
    except Exception:  # noqa: BLE001
        log.warning("judge: provider health unreadable for %s", finding.subject, exc_info=True)
        return ""
    if not rows:
        return ""
    newest = rows[-1]
    if not newest.get("ok"):
        return ""
    when = str(newest.get("probed_at") or "")
    if finding.last_at is not None and when:
        try:
            probed = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            return ""
        if probed <= finding.last_at:
            return ""
    return f"{finding.subject} answered its next check ({when or 'since'})"


def _drift_rated_ok_since(finding) -> str:
    """The drift check's newest word on this signal, across the whole log.

    `read_conscience` takes the latest row IN the window; this takes the
    latest row there is, which is what lets a check that re-ran after the
    window closed clear a warning inside it. A signal absent from the newest
    row is not resolved: the check may simply not have run it, and silence is
    not an `ok`.
    """
    from tesseract.orchestrator.watchman.sources import _parse_ts, _rows
    from tesseract.paths import log_dir

    directory = log_dir("conscience")
    if not directory.exists():
        return ""
    newest, newest_at = None, None
    try:
        for path in sorted(directory.glob("drift-*.jsonl")):
            for row in _rows(path):
                at = _parse_ts(row.get("timestamp"))
                if at is None:
                    continue
                if newest_at is None or at > newest_at:
                    newest, newest_at = row, at
    except OSError:
        return ""
    if newest is None or newest_at is None:
        return ""
    if finding.last_at is not None and newest_at <= finding.last_at:
        return ""
    for signal in newest.get("signals") or []:
        if str(signal.get("name") or "") != finding.subject:
            continue
        if str(signal.get("status") or "ok") != "ok":
            return ""
        return (
            f"the drift check rates {finding.subject} ok as of "
            f"{newest_at.isoformat(timespec='seconds')}"
        )
    return ""


def _pause_lifted(finding) -> str:
    """The governor's live set, not its log.

    Every other rule here reads a log forward from the finding. This one
    cannot: `pauses.jsonl` records the pause and the unpause as two events,
    and a pause added, lifted and re-added inside one window would read as
    cleared. `source-pauses.json` is what the kernel itself consults before
    dispatching, so absence from it is the answer.
    """
    import json

    from tesseract.orchestrator.autonomy.paths import source_pauses_path

    source = finding.subject.split("/", 1)[0]
    if not source:
        return ""
    path = source_pauses_path()
    if not path.exists():
        return f"the governor is not pausing {source} now"
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return ""
    paused = {
        str((entry or {}).get("source") or "")
        for entry in (raw.get("pauses") or [])
        if isinstance(entry, dict)
    }
    if source in paused:
        return ""
    return f"the governor is not pausing {source} now"


def _worker_kind_completed_since(finding) -> str:
    """A later terminal record of the same kind that ended healthily.

    The finding groups by `<kind>/<error class>`, and the class is not the
    question: the operator wants to know whether that kind of work runs. A
    later clean record of the kind says it does. A later FAILING record of the
    same class is another instance of the fault, and the collector already
    counted it.
    """
    from tesseract.orchestrator.watchman.sources import _parse_ts

    kind = finding.subject.split("/", 1)[0]
    if not kind or finding.last_at is None:
        return ""
    newest_at = None
    for record in _worker_records():
        if not record.is_terminal():
            continue
        if str(getattr(record.kind, "value", record.kind)) != kind:
            continue
        outcome = record.outcome
        if outcome is None or outcome not in HEALTHY_OUTCOMES:
            continue
        at = _parse_ts(getattr(record, "updated_at", None))
        if at is None or at <= finding.last_at:
            continue
        if newest_at is None or at > newest_at:
            newest_at = at
    if newest_at is None:
        return ""
    return (
        f"a {kind} worker has completed cleanly since "
        f"({newest_at.isoformat(timespec='seconds')})"
    )


def _nothing_is_stalled_now(finding) -> str:  # noqa: ARG001 — the set is the subject
    """The open set, asked again.

    The finding is about workers that were open with no heartbeat. It is a
    live question with a live answer, so it is asked rather than inferred.
    """
    from tesseract.orchestrator.workers.heartbeat import is_heartbeat_stale

    open_records = [r for r in _worker_records() if not r.is_terminal()]
    for record in open_records:
        try:
            if is_heartbeat_stale(record.id):
                return ""
        except OSError:
            return ""
    return "no worker is open without a heartbeat now"


def _worker_records() -> list:
    from tesseract.orchestrator.workers.record import list_active_records

    try:
        return list(list_active_records())
    except Exception:  # noqa: BLE001 — a stage that cannot read resolves nothing
        log.warning("judge: worker records unreadable", exc_info=True)
        return []


def _sweep_clean_since(finding) -> str:
    """The janitor's newest finished sweep, later than the finding."""
    from tesseract.orchestrator.watchman.sources import _parse_ts, _rows
    from tesseract.paths import log_dir

    path = log_dir("janitor") / "sweeps.jsonl"
    if not path.exists() or finding.last_at is None:
        return ""
    newest, newest_at = None, None
    try:
        for row in _rows(path):
            at = _parse_ts(row.get("finished_at_utc"))
            if at is None:
                continue
            if newest_at is None or at > newest_at:
                newest, newest_at = row, at
    except OSError:
        return ""
    if newest is None or newest_at is None or newest_at <= finding.last_at:
        return ""
    if newest.get("errors"):
        return ""
    return (
        f"the janitor has swept cleanly since "
        f"({newest_at.isoformat(timespec='seconds')})"
    )


def _breaker_reset(finding) -> str:
    """The breaker log's own last word on this breaker."""
    from tesseract.paths import log_dir

    path = log_dir("circuit-breakers") / f"{finding.subject}.jsonl"
    if not path.exists():
        return ""
    last = ""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped:
                last = stripped
    except OSError:
        return ""
    if not last:
        return ""
    import json

    try:
        event = str((json.loads(last) or {}).get("event") or "")
    except json.JSONDecodeError:
        return ""
    if event == "tripped":
        return ""
    return f"the {finding.subject} breaker has closed since ({event or 'reset'})"


__all__ = ["STAGE", "apply"]
