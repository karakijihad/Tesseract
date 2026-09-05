"""What the pipeline is doing, and what it did last time.

``GET /api/autonomy/pipeline`` feeds the operations strip: the run that is open
right now and the last one that closed, as two chains of steps. Both, always,
because a quiet morning otherwise looks identical to a morning after a night
that failed.

The strip renders states, not outcomes. The translation is
``orchestrator/liveness.py``'s and happens here rather than in TSX for the
reason the card contract gives: a view that decides what a record means is a
second description of the backend, and it goes stale on its own.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract import paths
from tesseract.orchestrator.liveness import OperationalState, state_of
from tesseract.scheduler.cadence import next_fire
from tesseract.scheduler.config_loader import load_schedule_config
from tesseract.scheduler.manifest.registry import entry as manifest_entry
from tesseract.scheduler.pipeline import stages as _stages  # noqa: F401 — registers the rows
from tesseract.scheduler.pipeline.graph import execution_order
from tesseract.scheduler.pipeline.manifest import (
    ManifestStore,
    RunManifest,
    is_single_stage,
)
from tesseract.scheduler.pipeline.registry import Row, find_stage, row as pipeline_row
from tesseract.scheduler.pipeline.stage import Stage

log = logging.getLogger(__name__)

# How far back to look for the last closed run before giving up. A day of
# single-stage runs by hand can sit in front of the newest row run, and reading
# every manifest ever written to answer one request is the kind of sweep that
# gets slower the longer the machine lives.
SCAN_LIMIT = 40

# Worst wins, and the order says why: something that errored outranks something
# that finished below contract, which outranks something that never began.
_SEVERITY: dict[OperationalState, int] = {
    OperationalState.NOT_INSTRUMENTED: 0,
    OperationalState.IDLE: 1,
    OperationalState.PENDING: 2,
    OperationalState.RUNNING: 3,
    OperationalState.UNKNOWN: 4,
    OperationalState.REFUSED: 5,
    OperationalState.DEGRADED: 6,
    OperationalState.FAILED: 7,
}


def row_of(manifest: RunManifest) -> Row | None:
    """The row this manifest ran, or None when nothing declares one.

    ``entry`` is the answer for anything written since the field existed. A
    manifest from before it falls back to the row that owns its first committed
    stage, which is why the fallback cannot be the only path: an open run with
    nothing committed has no first stage.

    It does NOT distinguish a single stage run by hand: that one bills to its
    row and carries the row's name, so it resolves here like any other.
    `manifest.is_single_stage` is what tells them apart.
    """
    if manifest.entry:
        found = pipeline_row(manifest.entry)
        if found is not None:
            return found
    for committed in manifest.rows:
        located = find_stage(committed.stage)
        if located is not None:
            return located[0]
    return None


def _ordered_stages(row: Row) -> tuple[Stage, ...]:
    """The row's stages in the order the runner walks them."""
    try:
        return execution_order(row.stages, external_reads=row.external_reads)
    except Exception:
        # A declaration that stopped ordering is the boot check's finding, not
        # this route's. Report the row as declared rather than 500 the panel
        # that exists to show something is wrong.
        log.exception("pipeline route: row %s does not order", row.name)
        return tuple(row.stages)


def _aware(value: datetime) -> datetime:
    """A manifest written by an older run can carry a naive stamp."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """Always with an offset. A naive stamp reaching the browser is read in
    whatever timezone the machine is in, which prints the wrong hour beside a
    run that is being read precisely for when it happened."""
    return _aware(value).isoformat() if value else None


def _in_flight(
    manifest: RunManifest, stage: Stage, now: datetime
) -> tuple[OperationalState, dict[str, str] | None, float]:
    """The state of the one stage an open run is sitting on.

    The pipeline has no heartbeat of its own, so its declared budget stands in
    for one: a stage past the time it said it needs has stopped reporting, and
    the contract is explicit that this does not keep looking lively.
    """
    last_sign = max(
        (_aware(r.ended_at) for r in manifest.rows if r.ended_at),
        default=_aware(manifest.started_at),
    )
    silent_for = (now - last_sign).total_seconds()
    if silent_for > stage.budget_seconds:
        return (
            OperationalState.DEGRADED,
            {
                "code": "heartbeat_overdue",
                "message": (
                    f"no sign of it for {int(silent_for)}s, and it declared "
                    f"{int(stage.budget_seconds)}s"
                ),
            },
            stage.budget_seconds,
        )
    return OperationalState.RUNNING, None, stage.budget_seconds


def _stage_payloads(manifest: RunManifest, now: datetime) -> list[dict[str, Any]]:
    row = row_of(manifest)
    committed = {r.stage: r for r in manifest.rows}
    if row is None:
        declared: tuple[Stage, ...] = ()
    else:
        declared = _ordered_stages(row)
    # A single stage run by hand belongs to no row; report what it committed.
    names = [s.name for s in declared] or list(committed)
    summaries = {s.name: s.summary for s in declared}
    budgets = {s.name: s.budget_seconds for s in declared}

    open_run = manifest.completed_at is None
    awaiting_turn = [
        name
        for name in names
        if name not in committed
        and name not in manifest.not_due
        and name not in manifest.disabled
    ]
    flight = awaiting_turn[0] if open_run and awaiting_turn else None

    out: list[dict[str, Any]] = []
    for name in names:
        record = committed.get(name)
        expected: float | None = None
        if record is not None:
            state = state_of(record.outcome)
            reason = (
                {"code": record.outcome.value, "message": record.reason}
                if record.reason
                else None
            )
            payload = {
                "state": state.value,
                "observedAt": _iso(record.ended_at) or _iso(record.started_at),
                "reason": reason,
                "source": "scheduler-run-record",
                "changed": record.changed,
                "refused": record.refused,
                "durationMs": record.duration_ms,
                "outcome": record.outcome.value,
            }
        elif name in manifest.disabled:
            payload = {
                "state": OperationalState.REFUSED.value,
                "observedAt": _iso(manifest.started_at),
                "reason": {
                    "code": "disabled",
                    "message": "turned off in this row's settings",
                },
                "source": "none",
                "changed": 0,
                "refused": 0,
                "durationMs": 0.0,
                "outcome": None,
            }
        elif name in manifest.not_due:
            payload = {
                "state": OperationalState.IDLE.value,
                "observedAt": _iso(manifest.started_at),
                "reason": {
                    "code": "not_due",
                    "message": "it had not come round again by this run's anchor",
                },
                "source": "none",
                "changed": 0,
                "refused": 0,
                "durationMs": 0.0,
                "outcome": None,
            }
        elif name == flight:
            state, reason, expected = _in_flight(
                manifest, next(s for s in declared if s.name == name), now
            )
            payload = {
                "state": state.value,
                "observedAt": _iso(manifest.started_at),
                "reason": reason,
                "source": "scheduler-run-record",
                "changed": 0,
                "refused": 0,
                "durationMs": 0.0,
                "outcome": None,
            }
        elif open_run:
            payload = {
                "state": OperationalState.PENDING.value,
                "observedAt": None,
                "reason": None,
                "source": "none",
                "changed": 0,
                "refused": 0,
                "durationMs": 0.0,
                "outcome": None,
            }
        else:
            # The run is closed and this step has no record, which means the
            # pass stopped short: the resume check closes an abandoned run
            # exactly as it stands. Not `pending` — the contract scopes that to
            # a step awaiting its turn in an OPEN run, and a night that ran
            # three of twenty-two steps read `idle` while it said so.
            payload = {
                "state": OperationalState.UNKNOWN.value,
                "observedAt": None,
                "reason": {
                    "code": "run_ended_first",
                    "message": "the run ended before this step was reached",
                },
                "source": "none",
                "changed": 0,
                "refused": 0,
                "durationMs": 0.0,
                "outcome": None,
            }
        payload["stage"] = name
        payload["summary"] = summaries.get(name, "")
        payload["expectedWithin"] = expected if expected is not None else budgets.get(name)
        out.append(payload)
    return out


def run_payload(manifest: RunManifest, now: datetime) -> dict[str, Any]:
    stages = _stage_payloads(manifest, now)
    # Everything decides how the run reads EXCEPT what did not happen this run
    # by design. A step the operator turned off, or one that was not due, would
    # otherwise make every night report the same thing forever. Grading on
    # "has a record of its own" instead was the bug: a step nobody reached has
    # no record either, so an abandoned pass graded as a clean one.
    excused = {"not_due", "disabled"}
    graded = [
        OperationalState(s["state"])
        for s in stages
        if (s["reason"] or {}).get("code") not in excused
    ]
    state = max(graded, key=lambda s: _SEVERITY[s]) if graded else OperationalState.IDLE
    row = row_of(manifest)
    name = manifest.entry or (row.name if row else "")
    declared = manifest_entry(name) if name else None
    started = _aware(manifest.started_at)
    # The span of the WORK, not of the file. A pass that ran its last stage at
    # 23:03 and was closed the next morning by the resume check took three
    # minutes; reporting eleven hours would say the opposite.
    last_seen = max(
        (_aware(r.ended_at) for r in manifest.rows if r.ended_at), default=None
    )
    if manifest.completed_at is None:
        ended = now
    else:
        ended = last_seen or _aware(manifest.completed_at)
    return {
        "runId": manifest.run_id,
        "entry": name,
        # The entry's own prose, rendered verbatim. Nothing here is written in
        # the view.
        "summary": declared.summary if declared else "",
        "why": declared.why if declared else "",
        "kind": declared.kind.value if declared else None,
        "owner": declared.owner.value if declared else None,
        "chains": list(declared.chains) if declared else [],
        "anchor": _iso(manifest.anchor),
        "startedAt": _iso(manifest.started_at),
        "completedAt": _iso(manifest.completed_at),
        "durationMs": (ended - started).total_seconds() * 1000.0,
        "open": manifest.completed_at is None,
        "state": state.value,
        "stages": stages,
    }


def _next_fire(name: str, now: datetime) -> str | None:
    """When this row is next due, off the operator's own schedule.

    Without it the strip's first line reads "nothing running" and stops there,
    which answers half the question: the other half is whether that is normal.
    """
    if not name:
        return None
    try:
        schedule = load_schedule_config(paths.config_dir())
    except Exception:
        # The schedule failing to load is the boot check's finding. The strip
        # says less rather than nothing.
        log.exception("pipeline route: could not read the schedule")
        return None
    job = next((j for j in schedule.jobs if j.name == name), None)
    if job is None or not job.cadence or not job.enabled:
        return None
    due = next_fire(job.cadence, now)
    return due.isoformat() if due else None


def read_manifest(path: Path) -> RunManifest | None:
    try:
        return RunManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        log.warning("pipeline route: unreadable manifest at %s", path)
        return None


def _last_closed(store: ManifestStore, exclude: str | None) -> RunManifest | None:
    """The newest closed run of a registered row.

    Single-stage runs by hand live in the same directory and are skipped: the
    strip's second line answers "did last night's pass finish", and a stage the
    operator ran on its own is not that pass.
    """
    runs = store.runs_dir
    if not runs.is_dir():
        return None
    try:
        newest = sorted(runs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        log.exception("pipeline route: could not list %s", runs)
        return None
    for path in newest[:SCAN_LIMIT]:
        manifest = read_manifest(path)
        if manifest is None or manifest.run_id == exclude:
            continue
        # A stage somebody ran on its own, from the CLI or from the Atlas
        # room's `redraw now`. It bills to the row and carries the row's name,
        # so `row_of` finds one and only the id says which kind of run it was.
        # Without this the strip's second line answers "did last night's pass
        # finish" with a single step run at noon.
        if is_single_stage(manifest):
            continue
        if row_of(manifest) is None:
            continue
        if manifest.entry and pipeline_row(manifest.entry) is None:
            continue
        return manifest
    return None


async def get_pipeline(request: web.Request) -> web.Response:
    """The open run and the last closed one, as the strip draws them."""
    del request
    now = datetime.now(timezone.utc)
    store = ManifestStore()
    current = store.load_open()
    previous = _last_closed(store, exclude=current.run_id if current else None)
    # Which row to say the next fire of: the one that is open, else the one
    # that last closed, so the line under a quiet strip is about the same row
    # the line above it named.
    named = current or previous
    row_name = ""
    if named is not None:
        row = row_of(named)
        row_name = named.entry or (row.name if row else "")
    return web.json_response(
        {
            "current": run_payload(current, now) if current else None,
            "previous": run_payload(previous, now) if previous else None,
            "nextFireAt": _next_fire(row_name, now),
            "observedAt": now.isoformat(),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/pipeline", get_pipeline)


__all__ = [
    "SCAN_LIMIT",
    "get_pipeline",
    "read_manifest",
    "register",
    "row_of",
    "run_payload",
]
