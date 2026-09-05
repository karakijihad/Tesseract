"""What the machine is doing on its own, and what it needs from you.

``GET /api/autonomy/overview`` is the room the panel opens on. Four figures and
three bands: what wants the operator, what is working right now, and what ran
while they were away.

**The join lives here rather than in the view.** Four producers write to
"wants you" and the operator should not have to know which: a step in the
night's run that did not succeed, an item blocked or waiting on their answer, a
source the governor paused, and a worker that died. That join used to happen in
TSX, which meant the panel's most important list was assembled by the one layer
with no access to what a state means.

**Working now has exactly one producer, and that is the point.** The activity
registry indexes every running unit of the assistant's work already: seats,
named lanes, delegations, scheduled routines, autonomy runs and MCP sessions.
Reading the three routes that feed it would be three answers to one question,
and a live worker is a sub-unit of the autonomy item that is already listed, so
counting it separately would show the same work twice at two grains.

**Ran while you were away is the Managed system reading.** Same rows, same
function that decides what a run's outcome was, because a second definition of
that would be the first one to drift.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web

from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_turn_step_expected_within_s,
)
from tesseract.mirror.server.routes._bands import band_for

from tesseract.mirror.server.routes import autonomy_managed as managed_route
from tesseract.mirror.server.routes import pipeline as pipeline_route
from tesseract.mirror.server.routes._isotime import iso as _iso
from tesseract.mirror.server.routes._isotime import parse as _parse
from tesseract.mirror.server.routes._isotime import took as _took
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.liveness import OperationalState, label_of
from tesseract.scheduler.cadence import next_fire

log = logging.getLogger(__name__)

# One unwrap for this room's producers, named so a warning says which surface
# went thin. Four rooms had four copies of it.
_band = band_for("overview")

# Steps in these states are healthy or have not had their turn yet. Everything
# else in a run is something that came back below succeeded.
QUIET_STEPS = frozenset({OperationalState.IDLE.value, OperationalState.PENDING.value})

# How many rows each band carries. The room's whole criterion is that it fits
# one screen at the panel size the operator meets it at, and a band that grows
# without limit is what puts a scrollbar back in front of them. What is dropped
# is counted out loud on the band rather than quietly left off.
BAND_ROWS = 8

# How long a RECORD OF A PAST EVENT stays on a list called "what needs you".
#
# A live condition never ages out however old it is: a paused source, a held
# item and a step wrong in the run that is open now are all still true. A dead
# worker and last night's step are records of something that already finished,
# and three eleven-day-old worker corpses at the top push the run that failed
# last night off the screen.
RECORD_WINDOW = timedelta(hours=48)

# How far back "ran while you were away" reaches.
AWAY_WINDOW = timedelta(hours=24)

# Worst first, and the order says why: something that errored outranks
# something that finished below contract, which outranks something that never
# began, which outranks something merely waiting on the operator.
RANK: dict[str, int] = {
    OperationalState.FAILED.value: 0,
    OperationalState.DEGRADED.value: 1,
    OperationalState.REFUSED.value: 2,
    OperationalState.UNKNOWN.value: 3,
    OperationalState.PENDING.value: 4,
}

# A worker in one of these came back without doing its job.
DEAD_WORKERS = frozenset({"failed", "tool_error", "interrupted"})

# An activity record in one of these is doing something right now. The rest of
# the registry is work that stopped and work that is merely open: it keeps a
# failed routine until it is dismissed, and a controller session stays `idle`
# for as long as nobody closes it. Four seats idle since this morning read as
# four things working, and the figure above them said five were running while
# one was, which is the reading this band exists to get right.
WORKING = frozenset({"running", "spawning", "input_required"})

# What an agenda item's status means about whether it wants the operator.
HELD_ON_SOMETHING = "blocked"
WAITING_ON_YOU = "awaiting_operator"


def line(
    *,
    name: str,
    state: OperationalState,
    said: str,
    at: datetime | None = None,
    value: str = "",
    opens: dict[str, str] | None = None,
    acts: tuple[str, ...] = (),
    acts_on: str | None = None,
) -> dict[str, Any]:
    """One row in a band.

    `label` travels with the state as it does in Health and in Managed system:
    a person reading a room needs words, and the translation belongs where the
    state is defined. `acts` names what may be done from the row itself, which
    is the backend's answer for the same reason `canRun` is over there.

    `acts_on` is what a verb is applied TO, when that is not what the row is
    called. It exists because the two used to be the same string: a paused
    source was named by its identifier, and `unpause` posted the row's name.
    Naming the row in words then silently posted the words. A row that says
    what it is and a verb that hits the right thing are two facts, so they are
    two fields, and the row is free to be readable.
    """
    return {
        "name": name,
        "state": state.value,
        "label": label_of(state),
        "said": said,
        "at": _iso(at),
        "value": value,
        "opens": opens,
        "acts": list(acts),
        "actsOn": acts_on if acts_on is not None else name,
    }


def _elapsed(since: datetime | None, now: datetime) -> str:
    """How long it has been going, in the operator's terms."""
    if since is None:
        return ""
    seconds = max(0.0, (now - since).total_seconds())
    if seconds < 90:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m"
    hours, rest = divmod(int(minutes), 60)
    return f"{hours}h {rest}m"


def held_items(items: list[Any]) -> list[dict[str, Any]]:
    """Every agenda item waiting on something, as a row.

    Shared by the Overview room's first band and by Blocked and paused, which
    is the room that exists to list exactly these. Two readings of one join,
    both the backend's, so the two rooms cannot disagree about what a held item
    is or what may be done to it.
    """
    out: list[dict[str, Any]] = []
    for item in items:
        if item.status == HELD_ON_SOMETHING:
            said = getattr(item, "blocked_reason", "") or item.rationale
            acts: tuple[str, ...] = ("resume", "cancel")
            state = OperationalState.REFUSED
        elif item.status == WAITING_ON_YOU:
            said, acts = item.rationale, ("approve", "cancel")
            state = OperationalState.PENDING
        else:
            continue
        # The GOAL is the sentence, so it goes where a sentence fits. A row's
        # name is a column of aligned short names, and an agenda goal put there
        # truncated to `act on he...` on three rows running, which identifies
        # nothing. What names the row is where it came from; what it IS is the
        # goal, and the reason it is held rides beside it.
        #
        # `.value` on the source because `AgendaSource` is an enum and its repr
        # is `AgendaSource.SELF_REFLECTION`, which reached the screen.
        out.append(
            line(
                name=str(getattr(item.source, "value", item.source)),
                state=state,
                said=item.goal,
                at=_parse(item.updated_at),
                value=said,
                opens={"kind": "agenda", "id": item.id},
                acts=acts,
            )
        )
    return out


#: What to say beside an open task, by its own status. Every row is `pending`:
#: a task has no heartbeat of its own, and the liveness contract lets a surface
#: claim `running` only on a fresh beat. When a turn IS working the task, the
#: turn appears under Working now with its beat; this row says only that work
#: is expected and what to do about it.
_OWED: dict[str, str] = {
    "proposed": "accepted, waiting for a conversation to take it up",
    "running": "taken up; a conversation is working it",
    "resume_queued": "cut off by a restart; take it up again",
}


def owed_items(items: list[Any]) -> list[dict[str, Any]]:
    """Every open task: what you asked for and the machine still owes you.

    A task is the one kind of item that is neither autonomy's own work nor
    something waiting on you, so the other bands had no place for it and an
    accepted task showed nowhere on this room. Its card opens from the row.
    Held tasks (blocked, waiting on you) stay in the held band, which is where
    a person looks for something that needs them.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        if getattr(item, "source", None) != AgendaSource.TASK:
            continue
        word = _OWED.get(item.status.value)
        if word is None:
            continue
        turns = len(getattr(item, "turn_ids", ()))
        rows.append(
            line(
                name=item.goal,
                state=OperationalState.PENDING,
                said=f"Done when: {item.success_criteria}" if item.success_criteria else item.rationale,
                at=item.updated_at,
                value=f"{word}; {turns} turn{'s' if turns != 1 else ''} so far" if turns else word,
                opens={"kind": "agenda", "id": item.id},
                acts=("cancel",),
            )
        )
    return rows


# Why the governor stopped a source, in words. The stored value is a code and
# it used to reach the screen as one: the band that exists to say what wants
# the operator read `loop_detected` next to `operator_view`, which is the
# machine's vocabulary twice over and tells a reader neither what happened nor
# what to do. Each says the consequence, then the remedy.
_WHY_PAUSED = {
    "loop_detected": (
        "it kept asking for the same thing, so the runtime stopped taking "
        "work from it. Nothing it suggests will reach you until you start it "
        "again"
    ),
    "cost_spiral": (
        "it was spending faster than its ceiling allows, so the runtime "
        "stopped taking work from it. Start it again once you are happy with "
        "what it costs"
    ),
    "trust_degraded": (
        "too much of what it asked for was turned down, so the runtime "
        "stopped taking work from it. Start it again if you want it back"
    ),
}


def _source_name(source: Any) -> str:
    """The source as a person reads it, never the identifier."""
    raw = str(getattr(source, "value", source))
    return raw.replace("_", " ")


def paused_rows(pauses: list[Any]) -> list[dict[str, Any]]:
    """Every source the governor stopped, as a row."""
    return [
        line(
            name=_source_name(pause.source),
            state=OperationalState.REFUSED,
            said=_WHY_PAUSED.get(
                pause.reason,
                # An unmapped code still beats no sentence, but it is framed
                # so the reader can tell it is a cause and not an instruction.
                f"the runtime stopped taking work from it, and recorded the "
                f"cause as {pause.reason}",
            ),
            at=_parse(pause.paused_at),
            value="paused",
            acts=("unpause",),
            acts_on=str(getattr(pause.source, "value", pause.source)),
        )
        for pause in pauses
    ]


def wants_you(
    pipeline: dict[str, Any],
    items: list[Any],
    pauses: list[Any],
    workers: list[Any],
    now: datetime,
) -> tuple[list[dict[str, Any]], int]:
    """Everything that came back below succeeded, from every producer with one.

    Ownership is deliberately NOT the axis. A shipped row that stopped firing
    and one the operator wrote that stopped firing are the same emergency; who
    wrote it is the second question and Managed system is where it is asked.

    Returns the rows and how many records aged out of the window, which is
    counted out loud because a queue that quietly forgets is the defect this
    surface exists to prevent.
    """
    rows: list[tuple[dict[str, Any], bool]] = []

    # The open run first, then the last closed one. A step still wrong in the
    # current run outranks the same step's ghost in last night's.
    for run in (pipeline.get("current"), pipeline.get("previous")):
        if not run:
            continue
        for step in run.get("stages", ()):
            if step["state"] in QUIET_STEPS:
                continue
            reason = (step.get("reason") or {}).get("message") or step.get("summary") or ""
            rows.append(
                (
                    line(
                        name=step["stage"],
                        state=OperationalState(step["state"]),
                        said=reason,
                        at=_parse(step.get("observedAt")),
                        value="running now" if run.get("open") else "last run",
                        # The step belongs to a declared entry, and the entry's
                        # card is what says what the step was for.
                        opens={"kind": "entry", "id": run["entry"]} if run.get("entry") else None,
                    ),
                    bool(run.get("open")),
                )
            )

    for row in held_items(items):
        rows.append((row, True))

    for row in paused_rows(pauses):
        rows.append((row, True))

    for worker in workers:
        if worker.status not in DEAD_WORKERS:
            continue
        rows.append(
            (
                line(
                    name=worker.role or worker.kind,
                    state=OperationalState.FAILED,
                    # The status is already the state word beside it. Saying it
                    # twice is how this once read "failed / coder_seat / failed".
                    said=getattr(worker, "error_message", "") or "",
                    at=_parse(worker.updated_at),
                    value=f"worker {worker.id}",
                    opens={"kind": "worker", "id": worker.id},
                ),
                False,
            )
        )

    kept: list[dict[str, Any]] = []
    aged = 0
    for row, live in rows:
        if live:
            kept.append(row)
            continue
        at = _parse(row["at"])
        if at is None or now - at <= RECORD_WINDOW:
            kept.append(row)
        else:
            aged += 1
    # Newest first, then worst first over that. Python's sort is stable, so the
    # second pass keeps the first one's order inside each rank.
    _newest_first(kept)
    kept.sort(key=lambda r: RANK.get(r["state"], 9))
    return kept, aged


def _newest_first(rows: list[dict[str, Any]]) -> None:
    """Most recent first. A row with no clock sorts last, because a fact with
    no time cannot claim to be the most recent one, and the empty string is
    what a missing stamp compares as under a descending sort."""
    rows.sort(key=lambda r: r["at"] or "", reverse=True)


def _turn_is_overdue(record: Any, now: datetime) -> bool:
    """Whether an open turn has gone quiet for longer than it promised.

    The liveness contract's rule, applied where a person is actually waiting:
    `running` needs a start event AND a fresh beat, and a unit that has not
    beaten within `expectedWithin` is `degraded` rather than going on looking
    lively. A turn beats by BEGINNING a step, so a turn wedged inside one slow
    call stops beating exactly while it looks busiest, which is the case this
    is here for.

    Only turns. Every other kind in the registry either beats on a cadence of
    its own or is a seat that is allowed to sit open, and reading a `lane` as
    overdue would say a chair nobody is in has gone wrong.
    """
    if record.kind != "turn":
        return False
    beat = _parse(record.updated_at)
    if beat is None:
        return False
    try:
        window = load_turn_step_expected_within_s(default_runtime_config_path())
    except (OSError, ValueError, KeyError):
        # A room says less rather than 500ing, and a missing dial must not be
        # the thing that decides a turn is unhealthy.
        log.exception("overview route: turn_step_expected_within_s is unreadable")
        return False
    return (now - beat).total_seconds() > window


def working_now(now: datetime) -> list[dict[str, Any]]:
    """Every running unit of the assistant's work, from the one index of them."""
    from tesseract.orchestrator.activity.registry import get_activity_registry

    try:
        snapshot = get_activity_registry().snapshot()
    except Exception:  # noqa: BLE001 — a room says less rather than 500ing
        log.exception("overview route: the activity registry could not be read")
        return []
    rows: list[dict[str, Any]] = []
    for record in snapshot:
        if record.state not in WORKING:
            continue
        started = _parse(record.started_at)
        overdue = _turn_is_overdue(record, now)
        if overdue:
            state = OperationalState.DEGRADED
        elif record.state == "running":
            state = OperationalState.RUNNING
        else:
            state = OperationalState.PENDING
        # A turn says what it is held on in `result`, because it has no goal
        # and `result` is where a row already looks for a subject. Read only
        # for a turn: on every other kind `result` is the terminal outcome
        # summary, and reading it here would change what six kinds of row say.
        said = record.goal or record.kind.replace("_", " ")
        if record.kind == "turn" and record.result:
            said = record.result
        if overdue:
            said = f"{said}, and it has been longer than expected"
        rows.append(
            line(
                name=record.label or record.activity_id,
                state=state,
                said=said,
                at=started,
                value=_elapsed(started, now),
            )
        )
    # Longest running first: what has been going for an hour and a half is more
    # of an answer to "what is my machine doing" than what started a minute ago.
    rows.sort(key=lambda r: (r["at"] is None, r["at"] or ""))
    return rows


def ran_while_away(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Every scheduled row's last run, in the reading Managed system uses.

    A row that has never fired is not on this band. "Ran while you were away"
    is a claim about something that happened, and a row with nothing behind it
    would be the one line here that is not.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        # A job going right now is on Working now. Listing its last run here as
        # well is the same work at two grains, which is the joining this room
        # exists to do for the operator rather than ask of them.
        if row["state"] == OperationalState.RUNNING.value:
            continue
        at = _parse(row["at"])
        if at is None or now - at > AWAY_WINDOW:
            continue
        out.append(
            line(
                name=row["name"],
                state=OperationalState(row["state"]),
                # The run's own reason, never Managed system's fallback to when
                # the row next fires. That sentence is the right answer to "what
                # is this row" and the wrong answer to "how did it go", and a
                # clean run that has nothing to report says nothing: its state
                # is already beside it, in a word and in a colour.
                said=row.get("reason", ""),
                at=at,
                value=row.get("took", ""),
                opens=row["opens"],
            )
        )
    _newest_first(out)
    return out


def figures(
    running: list[dict[str, Any]],
    waiting: list[dict[str, Any]],
    spend: dict[str, Any] | None,
    next_at: datetime | None,
) -> list[dict[str, Any]]:
    """The four numbers the room opens with.

    A figure nobody can source is dropped rather than published as zero, which
    is the rule the written lines already follow: `$0.00 today` on a machine
    with no ledger is a claim about spending, not an absence of one.
    """
    out: list[dict[str, Any]] = [
        {"key": "running", "value": str(len(running)), "label": "running", "warn": False},
        {
            "key": "wants",
            "value": str(len(waiting)),
            "label": "wants you",
            "warn": len(waiting) > 0,
        },
    ]
    if spend is not None:
        out.append(
            {
                "key": "cost",
                "value": f"${float(spend.get('spent_usd') or 0):.2f}",
                "label": "today",
                "warn": bool(spend.get("warning") or spend.get("blocked")),
            }
        )
    if next_at is not None:
        out.append(
            {
                "key": "next",
                "value": next_at.astimezone().strftime("%H:%M"),
                "label": "next pass",
                "warn": False,
            }
        )
    return out


def next_pass(scheduler: Any, now: datetime) -> datetime | None:
    """When the soonest enabled scheduled row comes round again.

    Enabled is read the way `schedule_line` reads it, from the config AND the
    engine's runtime state, because a row the operator turned off this morning
    is still enabled in the file it was loaded from and would otherwise be the
    figure.

    An event row declares no clock, so it is not a candidate: the figure says
    when the machine next acts on its own, and "waits for a leaf" is not a
    time. A machine with nothing on a cadence has no next pass and the figure
    is dropped rather than invented.
    """
    soonest: datetime | None = None
    for job in getattr(scheduler, "configs", ()) or ():
        try:
            runtime = scheduler.runtime_state(job.name) or {}
        except (KeyError, AttributeError):
            runtime = {}
        if not (bool(getattr(job, "enabled", True)) and bool(runtime.get("enabled", True))):
            continue
        cadence = runtime.get("cadence") or getattr(job, "cadence", "")
        if not cadence:
            continue
        due = next_fire(cadence, now)
        if due is not None and (soonest is None or due < soonest):
            soonest = due
    return soonest


def agenda_store(app: web.Application) -> AgendaStore:
    """The agenda, read through whatever the app already holds.

    Takes the app rather than the request because a request is no longer the
    only way this is reached: `autonomy_read` answers the same question from a
    tool call, and a reader that can only be reached by HTTP is a reader only
    the frontend has.
    """
    return app.get("agenda_store") or AgendaStore()


def paused_sources(app: web.Application) -> list[Any]:
    """Sources the governor paused. Read through the store the governor route
    reads, so the two cannot disagree about how many there are."""
    from tesseract.orchestrator.autonomy.governor import PauseStore

    store = app.get("autonomy_pause_store") or PauseStore()
    try:
        return list(store.all_paused().values())
    except Exception:  # noqa: BLE001 — a room says less rather than 500ing
        log.exception("overview route: the pause store could not be read")
        return []


def pipeline_payload(now: datetime) -> dict[str, Any]:
    """The open run and the last closed one, as the strip once read them.

    Called rather than copied, so the rail and this room cannot grade one night
    two ways.

    This is the third reader of the manifest directory: the map scans it too,
    on its own timer. Measured 2026-08-25 on a live tree, the scan is about
    10ms against `SCAN_LIMIT` of 40, so sharing one would cost more machinery
    than it saves. Written down so the next reader does not have to measure it
    again.
    """
    from tesseract.scheduler.pipeline.manifest import ManifestStore

    store = ManifestStore()
    current = store.load_open()
    previous = pipeline_route._last_closed(
        store, exclude=current.run_id if current else None
    )
    return {
        "current": pipeline_route.run_payload(current, now) if current else None,
        "previous": pipeline_route.run_payload(previous, now) if previous else None,
    }


def _spend(app: web.Application) -> dict[str, Any] | None:
    ledger = app.get("cost_ledger")
    if ledger is None:
        return None
    try:
        return dict(ledger.snapshot().get("global") or {})
    except Exception:  # noqa: BLE001 — a room says less rather than 500ing
        log.exception("overview route: today's spend could not be read")
        return None


def _schedule_rows(app: web.Application, now: datetime) -> list[dict[str, Any]]:
    """The managed reading of every scheduled row, with how long each took.

    `managed_route.schedules` is called rather than copied so the two rooms
    cannot grade one run two ways. What it does not carry is the duration and
    the run's own reason unmixed with Managed system's fallback, so the run log
    is read once more for those two fields.
    """
    from tesseract.scheduler.log import last_run_rows

    rows = managed_route.schedules(app, now)
    try:
        records = last_run_rows()
    except OSError:
        log.warning("overview route: the run log could not be read")
        records = {}
    for row in rows:
        record = records.get(row["name"]) or {}
        row["took"] = _took(record.get("duration_ms"))
        row["reason"] = str(record.get("outcome_reason") or "")
    return rows


async def get_overview(request: web.Request) -> web.Response:
    """The four figures and the three bands, each from its own producer."""
    now = datetime.now(timezone.utc)
    store = agenda_store(request.app)
    scheduler = request.app.get("scheduler")

    from tesseract.orchestrator.workers.record import list_active_records

    items, workers, pipeline, rows = await asyncio.gather(
        asyncio.to_thread(store.ranked),
        asyncio.to_thread(list_active_records),
        asyncio.to_thread(pipeline_payload, now),
        asyncio.to_thread(_schedule_rows, request.app, now),
        return_exceptions=True,
    )

    items = _band(items, [])
    workers = _band(workers, [])
    pipeline = _band(pipeline, {"current": None, "previous": None})
    rows = _band(rows, [])

    pauses = paused_sources(request.app)
    waiting, aged = wants_you(pipeline, items, pauses, workers, now)
    running = working_now(now)
    away = ran_while_away(rows, now)
    # Blocked and paused lists exactly these, and it lists ALL of them: a room
    # whose whole subject is what is held may not cap what it shows the way a
    # summary band does.
    held = held_items(items)
    _newest_first(held)
    owed = owed_items(items)
    _newest_first(owed)

    return web.json_response(
        {
            "figures": figures(
                running,
                waiting,
                _spend(request.app),
                next_pass(scheduler, now),
            ),
            "wantsYou": waiting[:BAND_ROWS],
            "wantsYouTotal": len(waiting),
            # Records of finished things older than the window. Counted, never
            # hidden without saying so.
            "aged": aged,
            "workingNow": running[:BAND_ROWS],
            "workingNowTotal": len(running),
            "ranWhileAway": away[:BAND_ROWS],
            "ranWhileAwayTotal": len(away),
            "held": held,
            "owed": owed,
            "paused": paused_rows(pauses),
            "observedAt": _iso(now),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/overview", get_overview)


__all__ = [
    "AWAY_WINDOW",
    "BAND_ROWS",
    "DEAD_WORKERS",
    "WORKING",
    "QUIET_STEPS",
    "RANK",
    "RECORD_WINDOW",
    "agenda_store",
    "figures",
    "get_overview",
    "held_items",
    "line",
    "next_pass",
    "owed_items",
    "paused_rows",
    "paused_sources",
    "pipeline_payload",
    "ran_while_away",
    "register",
    "wants_you",
    "working_now",
]
