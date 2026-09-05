"""One thing that runs on its own, and its own account of itself.

``GET /api/autonomy/entry/{name}`` is level 3 for every room on the Autonomy
panel: open a step in the strip, a chip on the map, a schedule in Managed
system, and this is what opens.

**Every word of it is the backend's.** `summary`, `why`, `kind`, `chain` and
`owner` are the manifest's, rendered verbatim, for the reason the card contract
gives: a job explained in the frontend is a job whose explanation goes stale
the first time the job changes. Nothing here composes a description.

The facts around the prose come from where each already lives. When it fires
lives in `schedule.yaml`, and which half of that the operator owns is
`config_loader.locked_fields_of`'s answer. How it went last time is the run
log's, read through `scheduler/log.py` rather than parsed a fourth time. What
it may spend is the ledger's own `per_role_caps`, which is the number actually
in force: the ceiling is declared in `roles.yaml` under the entry's own name,
and the ledger rebuilds that dict on every config reload. Reading the file here
would be a second reader that disagrees with the enforcer for as long as a
reload takes.

**What it spent comes from the same string as its ceiling.** The ledger keys
its running totals on the billing key, and the billing key IS the entry name,
so `_role_totals_usd[entry]` is this entry's spend today and
`per_role_caps[entry]` is what it is measured against. The two are read
together, from the object that does the refusing, so a card can never show a
figure from one source beside a limit from another.

This was written the other way round, and said the figure did not exist. It did
not, when spend was attributed per chain; it has since the billing key moved
onto the entry, and the sentence outlived the reason for it. An entry that has
not spent today reads `0.0`, which is a fact. `None` is kept for the case where
there is no ledger at all, which is a different thing and reads differently.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract import paths
from tesseract.mirror.server.routes._isotime import iso as _iso
from tesseract.mirror.server.routes._isotime import parse as _parse
from tesseract.mirror.server.routes._isotime import took as _took
from tesseract.orchestrator.liveness import label_of, state_of
from tesseract.scheduler.cadence import due_after_last
from tesseract.scheduler.config_loader import (
    load_schedule_config,
    locked_fields_of,
    system_rows,
)
from tesseract.scheduler.log import iter_runs, outcome_of_row, runs_path
from tesseract.scheduler.manifest.entry import (
    DISPATCHED,
    Entry,
    Runs,
    words_for,
    words_for_chain,
)
from tesseract.scheduler.manifest.registry import entry as manifest_entry

log = logging.getLogger(__name__)

# How many past runs the card shows. Enough to see a pattern, few enough that
# the answer to "how did it go" is not a log to read.
RECENT_RUNS = 10


def stage_summaries(name: str) -> dict[str, str]:
    """What each stage of this row is for, in the words the stage declares.

    `Stage.summary` is where that fact lives, and the Guide's table is the
    other renderer of it. A row that declares no stages, or a name that is not
    a row at all, answers with nothing rather than raising: most entries are
    not rows.
    """
    from tesseract.scheduler.pipeline import registry
    from tesseract.scheduler.pipeline import stages as _stages  # noqa: F401

    row = registry.row(name)
    return {stage.name: stage.summary for stage in (row.stages if row else ())}


def stages_of(payload: Any, summaries: dict[str, str]) -> list[dict[str, Any]]:
    """The steps one run took, as the run itself recorded them.

    Read from the run log's own row rather than joined from the manifest
    store: `row_job.py` puts the whole stage list in `payload`, so the record
    that produced this history line already carries what happened inside it.
    The pipeline route answers the same question for the CURRENT run and the
    last closed one, which is two of the ten a card lists.
    """
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    for raw in payload.get("stages") or ():
        if not isinstance(raw, dict):
            continue
        outcome = outcome_of_row(raw)
        state = state_of(outcome)
        out.append(
            {
                "stage": str(raw.get("stage") or ""),
                # What it is FOR, which is the half a list of names cannot
                # answer. Empty for a stage that has been renamed or removed
                # since the run, and the card says the name alone.
                "summary": summaries.get(str(raw.get("stage") or ""), ""),
                "outcome": outcome.value,
                "state": state.value,
                "label": label_of(state),
                "reason": str(raw.get("reason") or ""),
                "changed": int(raw.get("changed") or 0),
                "refused": int(raw.get("refused") or 0),
                # In words, because the panel's rule is that they are the
                # backend's. The same reading Overview's rows get.
                "took": _took(raw.get("duration_ms")),
            }
        )
    return out


def recent_runs(name: str, limit: int = RECENT_RUNS) -> list[dict[str, Any]]:
    """This entry's last runs, newest first, as the log recorded them.

    `outcome_of_row` decides what a row means, in the module that writes one.
    This is deliberately not a fourth reader of that format.
    """
    mine: list[dict[str, Any]] = []
    summaries = stage_summaries(name)
    try:
        rows = list(iter_runs(runs_path()))
    except OSError:
        # The card is the manifest's account of the entry, and that does not
        # depend on the log. A history nobody could read is an empty history
        # here and a finding for the Health room, not a 500 on the one screen
        # that explains what this thing is for.
        log.warning("entry route: the run log could not be read for %s", name)
        return []
    for row in rows:
        if row.get("job_name") != name:
            continue
        outcome = outcome_of_row(row)
        state = state_of(outcome)
        # `detail` is documented as free plain language, and one writer sets it
        # to a string rather than to the mapping the scheduler usually writes.
        detail = row.get("detail")
        trigger = detail.get("trigger_source") if isinstance(detail, dict) else None
        mine.append(
            {
                "runId": row.get("run_id"),
                "firedAt": row.get("fired_at"),
                "completedAt": row.get("completed_at"),
                "outcome": outcome.value,
                "state": state.value,
                "label": label_of(state),
                # The runtime's own sentence about the run, never the free text
                # riding beside it.
                "reason": str(row.get("outcome_reason") or ""),
                "trigger": str(trigger or ""),
                # A row is a subgraph, and its one line said so only by
                # accident: `outcome_reason` is `<stage>: <what it said>` for
                # the worst stage, so the card showed a stage name with no
                # sign that stages existed. These are the rest of them.
                "stages": stages_of(row.get("payload"), summaries),
                # Declared stages that did not run this time, which is a third
                # answer to "did it run" and not a failure. Names only: what
                # each is for is on the stage that has one.
                "notDue": [
                    str(x) for x in ((row.get("payload") or {}).get("not_due") or ())
                ] if isinstance(row.get("payload"), dict) else [],
                "turnedOff": [
                    str(x) for x in ((row.get("payload") or {}).get("disabled") or ())
                ] if isinstance(row.get("payload"), dict) else [],
            }
        )
    return mine[-limit:][::-1]


def row_named(name: str) -> Any | None:
    """The operator's own schedule row of that name, or None.

    One read of the schedule per request. `yours` and `schedule_of` each loaded
    and scanned the whole config for the same job, which is two full yaml
    parses on the loop that also carries health, the socket and inbound turns.
    """
    try:
        config = load_schedule_config(paths.config_dir())
    except Exception:
        # The schedule failing to load is the boot check's finding. The card
        # says less rather than nothing.
        log.exception("entry route: could not read the schedule")
        return None
    return next((j for j in config.jobs if j.name == name), None)


def last_fire_of(runs: list[dict[str, Any]], app: Any = None, name: str = "") -> datetime | None:
    """When this entry last fired. The engine first, then the log.

    An interval's next fire is measured from it, so the card needs it for the
    same reason the Managed room does, and in the same order: the engine stamps
    `last_fired_at` when it CLAIMS the job, the log is only appended when the
    run finishes. Reading the log alone meant a job in its first run, or a run
    slower than its own interval, had no last fire at all and fell back to the
    clock, which is the defect this argument exists to close.
    """
    live = _live_state(app, name)
    fired = _parse(live.get("last_fired_at")) if live else None
    if fired is not None:
        return fired
    for run in runs:
        fired = _parse(run.get("firedAt"))
        if fired is not None:
            return fired
    return None


def _live_state(app: Any, name: str) -> dict[str, Any] | None:
    """The engine's own runtime row for this entry, or None if it has none."""
    scheduler = app.get("scheduler") if hasattr(app, "get") else None
    if scheduler is None or not name:
        return None
    try:
        return scheduler.runtime_state(name)
    except (KeyError, AttributeError):
        return None


def schedule_of(
    name: str,
    now: datetime,
    job: Any | None = None,
    last_fired_at: datetime | None = None,
) -> dict[str, Any] | None:
    """What the operator's own schedule says about this entry.

    None for anything that does not fire on a clock: a service runs for as long
    as the app does, and a row that declares `when:` waits on an event and has
    no next time to compute. `JobConfig` enforces exactly one of the two, so a
    cadence field on an event row would render a broken clock where the
    condition's own sentence belongs.

    The next fire comes from `due_after_last` and not from the clock, because
    an interval is measured from its last fire: a five minute row last fired at
    12:00 is due at 12:05, and answering `now + 5m` showed a future time on a
    row the engine already held overdue.
    """
    job = job if job is not None else row_named(name)
    if job is None or not (job.cadence or "").strip():
        return None
    due = due_after_last(job.cadence, last_fired_at, now) if job.enabled else None
    return {
        "cadence": job.cadence,
        "enabled": bool(job.enabled),
        "nextFireAt": _iso(due),
    }


def yours(
    name: str,
    now: datetime,
    ceiling: float | None = None,
    app: Any = None,
) -> dict[str, Any] | None:
    """A card for a row the OPERATOR wrote, or None if they wrote no such row.

    The manifest declares what the app ships, so a job the operator created has
    no entry in it and never will. It still runs on its own, so it still opens,
    and the honest card is the one that renders their own summary and states
    the fields the app cannot fill rather than inventing them. `declared` is
    what says which of the two this is, so the view renders one shape.
    """
    job = row_named(name)
    if job is None:
        return None
    # A row fires on a clock or on an event and declares exactly one of the
    # two. Calling every one of them a `row` printed a cadence field on an
    # event row, which reads as a schedule that was never set.
    fires_on_event = bool((job.when or "").strip())
    runs = Runs.TRIGGER if fires_on_event else Runs.ROW
    history = recent_runs(name)
    return {
        "name": name,
        "declared": False,
        # Their words, from their own file. The two doors the app owns require
        # a summary; a hand-edited file is reported rather than refused, so a
        # blank one is possible and says so.
        "summary": job.summary or "",
        "why": "",
        "kind": None,
        "owner": None,
        "runs": runs.value,
        "kindLabel": None,
        "ownerLabel": None,
        "runsLabel": words_for(runs),
        # What the condition is called, for a row that waits on one. The card
        # has nowhere else to say what fires it.
        "firesOn": job.when or None,
        "chains": [],
        "chainLabels": [],
        "site": None,
        "substrate": None,
        # The ledger keys caps on one string whether the block is a seat or an
        # entry, so a row the operator capped in `roles.yaml` has a real limit
        # being enforced against it. Reporting None here said it had none while
        # the ledger refused its calls, which is the room contradicting the
        # runtime doing the refusing. Showing the number and not offering the
        # control are two decisions, and only the second one is below.
        "dailyBudgetUsd": ceiling,
        "spentToday": spent_today(app, name),
        "schedule": schedule_of(name, now, job, last_fire_of(history, app, name)),
        # Their row, so all of it is theirs. What the app ships is the only
        # thing the ownership rule locks, and this is not that.
        "canSetCadence": not fires_on_event,
        "cadenceNotice": None,
        # The app declares what it ships, so it has no ceiling to offer on a
        # row it did not write. Giving one a block in `roles.yaml` is the way
        # in, and that is a decision about their own file rather than a
        # control the card can honestly draw.
        "canSetCeiling": False,
        "recent": history,
        "observedAt": _iso(now),
    }


def ceiling_of(app: Any, name: str) -> float | None:
    """The most this entry may spend in a day, or None if it has no ceiling.

    From the ledger rather than from `roles.yaml`, because the ledger is what
    refuses the call. It keys caps on one string whether the block is a seat or
    an entry, which is why an entry's ceiling can live beside the seats at all.
    """
    ledger = app.get("cost_ledger") if hasattr(app, "get") else None
    if ledger is None:
        return None
    cap = getattr(ledger, "per_role_caps", {}).get(name)
    return float(cap) if cap is not None else None


def spent_today(app: Any, name: str) -> float | None:
    """What this entry has spent today, or None when nothing is metering.

    Same object and same key as `ceiling_of`, deliberately: a number taken from
    somewhere else could sit beside a ceiling it was never measured against.
    Zero is an answer and reads as one; None means there is no ledger, which
    the card says differently.
    """
    ledger = app.get("cost_ledger") if hasattr(app, "get") else None
    if ledger is None:
        return None
    totals = getattr(ledger, "_role_totals_usd", None)
    if totals is None:
        return None
    return float(totals.get(name, 0.0))


def chain_words(chains: tuple[str, ...]) -> list[str]:
    """Each chain this entry rides, in words a reader can act on.

    Off `load_bundle`, which is what every chain-building call in the runtime
    already reads: two file reads, and fresh, so the models named here are the
    ones the next call will actually try. `app["config"]` is a `ServerConfig`
    and its `models` view has no chains in it at all, which is how the first
    version of this printed the slug it exists to replace.

    Called from `card`, on a thread. A ref that no longer resolves is dropped
    from the sentence and not from the chain, so the count still stands.
    """
    from tesseract.brain.boot import load_bundle
    from tesseract.config.loader import ConfigError

    try:
        bundle = load_bundle()
        declared = (bundle.roles_raw.get("chains") or {})
    except Exception:  # noqa: BLE001 — a card says less rather than 500ing
        log.warning("entry route: the chains could not be read")
        return [words_for_chain(chain) for chain in chains]
    out: list[str] = []
    for chain in chains:
        models: list[str] = []
        for ref in declared.get(chain) or ():
            try:
                models.append(bundle.resolve(str(ref)).model.model)
            except ConfigError:
                continue
        out.append(words_for_chain(chain, models))
    return out


def card(
    entry: Entry, now: datetime, ceiling: float | None = None, app: Any = None
) -> dict[str, Any]:
    """The entry's own words, and the facts around them."""
    runs = recent_runs(entry.name)
    schedule = (
        schedule_of(entry.name, now, None, last_fire_of(runs, app, entry.name))
        if entry.runs is Runs.ROW
        else None
    )
    # Which of this row's fields the app decides, asked of the loader that
    # enforces it rather than worked out here. On a dev checkout there is one
    # config tree and nothing is the app's, which is the same answer
    # `removable` gives on the schedule route.
    locked = locked_fields_of(system_rows().get(entry.name))
    return {
        "name": entry.name,
        # Declared by the app, so every field below is the manifest's.
        "declared": True,
        # Verbatim from `scheduler/manifest/registry.py`. Nothing here is
        # written in the view, and nothing here is written in this file.
        "summary": entry.summary,
        "why": entry.why,
        "kind": entry.kind.value,
        "owner": entry.owner.value,
        "runs": entry.runs.value,
        # The words for each, so the view prints none of its own. `on_demand`
        # and `remote_model` are slugs, and the translation belongs where the
        # enum is defined rather than in TSX.
        "kindLabel": words_for(entry.kind),
        "ownerLabel": words_for(entry.owner),
        "runsLabel": words_for(entry.runs),
        "chains": list(entry.chains),
        # The same rule as `kindLabel`: the slug is what the code keys on and
        # the words are what a surface prints. `DISPATCHED` is the asterisk
        # that reached the card for months, and a bare `chain_1` is a name
        # only `roles.yaml` understands.
        "chainLabels": chain_words(entry.chains),
        "site": entry.site or None,
        "substrate": entry.substrate or None,
        # The most it may spend in a day. Null means no ceiling of its own; the
        # global cap still applies.
        "firesOn": entry.fires or None,
        "dailyBudgetUsd": ceiling,
        # What it spent, off the same key and the same object as the ceiling
        # above. An entry that names only `DISPATCHED` bills to whatever work
        # it started rather than to itself, so its own total stays at zero and
        # the card says as much beside a ceiling it also does not have.
        "spentToday": spent_today(app, entry.name),
        "schedule": schedule,
        # What may be CHANGED is the backend's answer, on the entry, for the
        # reason the managed room already gives: a view that decided it would
        # draw a control the write then refuses.
        #
        # Three ways there is no cadence to set here, and only the first is
        # about ownership. A row the app ships on a RATE keeps that rate,
        # because how often the app's own work runs is part of what the work
        # is; the one it fires at an hour of the day leaves that hour to the
        # operator. A row waiting on an event has no clock at all, and
        # `set_cadence` refuses one because two firing rules is not a schedule.
        # Anything that is not a row has no schedule to begin with.
        "canSetCadence": schedule is not None and "cadence" not in locked,
        # Said out loud where the control would have been, so the operator
        # meets one sentence at the point of the edit rather than a field that
        # quietly does not respond. Null wherever the control is drawn.
        "cadenceNotice": (
            "How often this one runs is set by the app, so an update can "
            "change it. You can still turn it off."
            if schedule is not None and "cadence" in locked
            else None
        ),
        # Two conditions, and both were learned the hard way.
        #
        # There has to BE a ceiling under this name, because that is the same
        # set `POST /api/settings/cost` will write: it accepts a name the
        # loaded roles bundle declares and refuses everything else. `pty_feed`
        # and `observer_subscriber` are billed entries riding a real chain with
        # no block of their own, so a control drawn for them offered an edit
        # the route answers with `unknown role`.
        #
        # And the entry has to bill AGAINST ITS OWN NAME. One that names
        # nothing but `DISPATCHED` rides whichever chain the work it runs
        # chose, and bills to that work, so a cap filed here would never be
        # read. It would not sit harmlessly either: the global ceiling is the
        # sum of every per-role cap, so a number set here loosens the one limit
        # that does apply while appearing to tighten one that does not.
        "canSetCeiling": ceiling is not None
        and any(chain != DISPATCHED for chain in entry.chains),
        "recent": runs,
        "observedAt": _iso(now),
    }


async def get_entry(request: web.Request) -> web.Response:
    """One entry's card, or a 404 naming what it looked in."""
    name = request.match_info.get("name", "")
    now = datetime.now(timezone.utc)
    found = manifest_entry(name)
    # Reading every row of the run log is file IO in the tens of milliseconds,
    # and the loop carries health, the socket and inbound turns.
    if found is not None:
        ceiling = ceiling_of(request.app, found.name)
        return web.json_response(
            await asyncio.to_thread(card, found, now, ceiling, request.app)
        )
    mine = await asyncio.to_thread(
        yours, name, now, ceiling_of(request.app, name), request.app
    )
    if mine is None:
        return web.json_response(
            {
                "error": (
                    f"nothing called {name!r} runs on its own. What the app runs "
                    "is declared in the manifest and what you run is in your "
                    "own schedule, and it is in neither"
                )
            },
            status=404,
        )
    return web.json_response(mine)


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/entry/{name}", get_entry)


__all__ = [
    "RECENT_RUNS",
    "card",
    "ceiling_of",
    "chain_words",
    "get_entry",
    "last_fire_of",
    "recent_runs",
    "register",
    "stage_summaries",
    "stages_of",
    "row_named",
    "schedule_of",
    "yours",
]
