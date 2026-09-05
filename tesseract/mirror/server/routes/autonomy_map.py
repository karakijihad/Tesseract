"""``GET /api/autonomy/map`` — the machine's shape, with live state on it.

The floor plan opens on a map: every way in, the one session they all arrive
at, what that session reaches, and what runs without anybody asking. This
serves it, and it invents none of it. The funnel comes from
``orchestrator/funnel.py`` and the things that run themselves come from
``scheduler/manifest/registry.py``, which is also where their prose comes
from. Nothing on the picture is written here.

**What is painted, and what is not.** A node reports ``not_instrumented`` until
something in the runtime produces a signal for it, per
``_shared/liveness-contract.md``: no producer renders as unwired, never as
quiet. Today that is every node of the funnel — the doors, the session and
what it reaches have no liveness producer — and the things that fire work do
have one, so those are the nodes that carry state. A map that painted the rest
green would be answering a question nothing asks.

**Two stores, because the machine keeps two.** A pipeline row writes a run
manifest; every other scheduled thing writes a line in the scheduler's own
``runs.jsonl``. Reading only the first was this route's own version of the
defect it exists to expose: `watchman` runs every quarter of an hour and the
map said no run of it had ever been recorded. The newer of the two records
wins, and where neither has anything the node says which kind of nothing it
is.

The run derivation is ``routes/pipeline.py``'s, called rather than copied: the
strip and the map read the same night, and two derivations of "how did that
run go" is two answers to one question. A logged run is read through
``scheduler/log.py``, which owns what a row is.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes.pipeline import (
    SCAN_LIMIT,
    read_manifest,
    row_of,
    run_payload,
)
from tesseract.orchestrator.funnel import BAND_LABELS, EDGES, NODES, Band
from tesseract.orchestrator.liveness import OperationalState, label_of, state_of
from tesseract.scheduler.log import last_run_rows, outcome_of_row
from tesseract.scheduler.manifest.entry import Entry, Runs
from tesseract.scheduler.manifest.registry import ENTRIES
from tesseract.scheduler.pipeline import stages as _stages  # noqa: F401 — registers the rows
from tesseract.scheduler.pipeline.manifest import ManifestStore, RunManifest

log = logging.getLogger(__name__)

# What the `runs` band holds: the things that fire work. A service is a loop
# the app keeps running rather than a piece of work with an outcome, and
# eighteen of them across the bottom of a map would bury the four rows that
# have a night to report. They are the Health room's, not the map's.
_FIRES_WORK = (Runs.ROW, Runs.TRIGGER, Runs.ON_DEMAND)


def _unwired() -> dict[str, Any]:
    """A node nothing reports on. Not quiet, and never green."""
    return {
        "state": OperationalState.NOT_INSTRUMENTED.value,
        "label": label_of(OperationalState.NOT_INSTRUMENTED),
        "observedAt": None,
        "reason": {"code": "no_producer", "message": "no runtime signal"},
        "source": "none",
    }


def _no_record(truncated: bool) -> dict[str, Any]:
    """No run of this entry was found, which is two different things.

    If the whole listing was read, nothing has ever run it and that is `idle`:
    instrumented, and it has not happened. If the scan stopped at its limit,
    the record may be further down and the honest answer is `unknown` — the
    contract is explicit that a gap is never filled with the state the reader
    would prefer.
    """
    if truncated:
        return {
            "state": OperationalState.UNKNOWN.value,
            "label": label_of(OperationalState.UNKNOWN),
            "observedAt": None,
            "reason": {
                "code": "not_in_recent_runs",
                "message": f"nothing about it in the last {SCAN_LIMIT} runs",
            },
            "source": "none",
        }
    return {
        "state": OperationalState.IDLE.value,
        "label": label_of(OperationalState.IDLE),
        "observedAt": None,
        "reason": {"code": "never_run", "message": "no run of it has been recorded"},
        "source": "scheduler-run-record",
    }


def _entry_of(manifest: RunManifest) -> str:
    """Which entry this run was. A manifest written before the field existed
    carries no name, and the row that owns its first committed stage is the
    same answer the strip reads — a run of `consolidate` is a run of
    `consolidate` whether or not it said so at the time.
    """
    if manifest.entry:
        return manifest.entry
    row = row_of(manifest)
    return row.name if row else ""


def _newest_by_entry(now: datetime) -> tuple[dict[str, dict[str, Any]], bool]:
    """The most recent run of each entry, as the strip reads a run.

    One scan of the manifest directory, bucketed, rather than one scan per
    node: the map asks about eight entries and the answer for all of them is
    in the same newest-first listing. Returns what was found and whether the
    scan stopped at its limit, because those are different answers about an
    entry that is missing from it.
    """
    store = ManifestStore()
    found: dict[str, RunManifest] = {}
    truncated = False
    open_run = store.load_open()
    if open_run is not None:
        named = _entry_of(open_run)
        if named:
            found[named] = open_run

    runs = store.runs_dir
    if runs.is_dir():
        try:
            newest = sorted(
                runs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except OSError:
            log.exception("map route: could not list %s", runs)
            newest = []
        truncated = len(newest) > SCAN_LIMIT
        for path in newest[:SCAN_LIMIT]:
            manifest = read_manifest(path)
            if manifest is None:
                continue
            named = _entry_of(manifest)
            if named:
                found.setdefault(named, manifest)

    return {name: run_payload(m, now) for name, m in found.items()}, truncated


def _from_manifest(run: dict[str, Any]) -> dict[str, Any]:
    state = OperationalState(run["state"])
    return {
        "state": state.value,
        "label": label_of(state),
        "observedAt": run["completedAt"] or run["startedAt"],
        "reason": None,
        "source": "scheduler-run-record",
    }


def _from_log(row: dict[str, Any]) -> dict[str, Any]:
    """One line of the scheduler's run log, read as a state."""
    state = state_of(outcome_of_row(row))
    said = str(row.get("outcome_reason") or "").strip()
    return {
        "state": state.value,
        "label": label_of(state),
        "observedAt": row.get("completed_at") or row.get("fired_at"),
        "reason": {"code": outcome_of_row(row).value, "message": said} if said else None,
        "source": "scheduler-run-record",
    }


def _when(liveness: dict[str, Any]) -> datetime | None:
    stamp = liveness.get("observedAt")
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _entry_node(
    entry: Entry,
    run: dict[str, Any] | None,
    logged: dict[str, Any] | None,
    *,
    truncated: bool,
) -> dict[str, Any]:
    # The newer record wins. A row that writes both leaves one of them behind
    # the moment it is run any other way, and the older one is not what
    # happened last.
    candidates = [
        c for c in (
            _from_manifest(run) if run else None,
            _from_log(logged) if logged else None,
        ) if c is not None
    ]
    liveness = max(
        candidates, key=lambda c: _when(c) or datetime.min.replace(tzinfo=timezone.utc)
    ) if candidates else _no_record(truncated)
    return {
        "name": entry.name,
        "band": Band.RUNS.value,
        # The entry's own prose, rendered verbatim.
        "summary": entry.summary,
        "why": entry.why,
        "kind": entry.kind.value,
        "owner": entry.owner.value,
        "runs": entry.runs.value,
        "site": entry.site or None,
        "liveness": liveness,
        # A node is the handle on the thing it stands for: opening one opens
        # that entry's card, one level down.
        "opens": {"kind": "entry", "id": entry.name},
        "lastRunId": run["runId"] if run else None,
    }


def _funnel_nodes() -> list[dict[str, Any]]:
    return [
        {
            "name": node.name,
            "band": node.band.value,
            "summary": node.summary,
            "site": node.site,
            "groups": list(node.groups),
            "liveness": _unwired(),
            "opens": None,
        }
        for node in NODES
    ]


async def get_map(request: web.Request) -> web.Response:
    """Every declared node and edge, with state where something produces it."""
    del request
    now = datetime.now(timezone.utc)
    runs, truncated = _newest_by_entry(now)
    logged = last_run_rows()
    nodes = _funnel_nodes() + [
        _entry_node(e, runs.get(e.name), logged.get(e.name), truncated=truncated)
        for e in ENTRIES
        if e.runs in _FIRES_WORK
    ]
    return web.json_response(
        {
            "bands": [
                {"id": band.value, "label": BAND_LABELS[band]} for band in Band
            ],
            "nodes": nodes,
            "edges": [
                {"source": e.source, "target": e.target, "carries": e.carries}
                for e in EDGES
            ],
            "observedAt": now.isoformat(),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/map", get_map)


__all__ = ["get_map", "register"]
