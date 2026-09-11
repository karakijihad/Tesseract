"""Everything the app manages for you, and everything you told it to manage.

``GET /api/autonomy/managed`` is the Managed system room: the scheduled rows,
the agents, the alarms and the playbooks, one list each, with who wrote a row
marked on the row. Ownership is the axis because it is the one that decides
what happens on an update. What the app ships is the app's to change; what the
operator wrote is theirs forever.

**The state and the words are the backend's, as everywhere else on this
panel.** A room that read `last_result.ok` and decided for itself what a
failure looks like would be a second definition of a run's outcome, and the
first one to drift: `scheduler/log.py::outcome_of_row` already says what a row
means and `liveness.py` already says what to call it. Only the field names on
the row are the view's.

Where a job last fired comes from two places on purpose. The run log has the
record; the engine has the clock it fired at. A row the log no longer carries
because a retention sweep aged it out is `unknown` rather than never run, which
is the distinction the map had to learn the hard way.

Anonymous-readable, like the other dashboard feeds. Every action on a row is a
separate write on its own existing route.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes._isotime import iso as _iso
from tesseract.mirror.server.routes._isotime import parse as _parse
from tesseract.orchestrator.liveness import OperationalState, label_of, state_of
from tesseract.orchestrator.obligation import as_payload as wants
from tesseract.orchestrator.watchman.tracker import TAG_BUILT_IN, TAG_CUSTOM
from tesseract.scheduler.cadence import due_after_last, in_words, when_words
from tesseract.scheduler.log import last_run_rows, outcome_of_row

log = logging.getLogger(__name__)

# What the app ships and what you wrote, in the order they are read. The app's
# own work first because it is the larger half and the half nobody edits.
SYSTEM = "system"
USER = "user"



def tag_of(origin: str) -> str:
    """The word for who wrote it, on the row.

    From `tracker.py`, because `WHAT-RUNS.md` draws the same split and the two
    surfaces said it in two vocabularies until the operator read their own row
    under a heading neither file used. One owner, one pair of words, wherever
    the split is drawn.

    A branch rather than a lookup on purpose: this runs for every row in the
    room, and a table would answer an origin nobody declared with a KeyError
    that the roster's own guard turns into an empty tab. The room whose job is
    to say what is running must not go blank over one unfamiliar word.
    """
    return TAG_BUILT_IN if origin == SYSTEM else TAG_CUSTOM


def line(
    *,
    name: str,
    origin: str,
    state: OperationalState,
    said: str,
    at: datetime | None = None,
    value: str = "",
    tag: str,
    opens: dict[str, str] | None = None,
    # What a model is given in place of `said`. `None` means both readers get
    # the same line. Named and shaped after `routes/autonomy_health.py`, so
    # the panel holds one answer to this rather than one per room.
    for_model: str | None = None,
    enabled: bool = True,
    can_run: bool = False,
    can_toggle: bool = False,
    can_delete: bool = False,
    ended: bool = False,
    awaiting_operator: bool = False,
) -> dict[str, Any]:
    """One row in the room.

    `tag` has no default on purpose. It is who wrote the row, and whether that
    is a question at all is the roster's answer: two of the three have both
    halves and the alarms have one, so nothing ships an alarm and every row
    there would carry the same word. A default would let the next roster added
    here be unmarked by omission rather than by decision.

    `label` travels with the state for the reason it does in Health: a person
    reading a room needs words, and the translation belongs where the state is
    defined. What the row may DO travels with it too, because whether a shipped
    job can be deleted is the backend's answer and a view that decided it would
    offer a control the route then refuses.
    """
    return {
        "name": name,
        "origin": origin,
        # On every row rather than over a band of them. A heading answers the
        # question only while the row is under it, and the same bit has to
        # survive a row read on its own. Blank on a roster where it cannot
        # vary: a mark that is the same on every line is chrome, and the view
        # narrows a row to make space for one.
        "tag": tag,
        "state": state.value,
        "label": label_of(state),
        # What the row asks of whoever reads it, and the only input to its
        # colour. A row somebody turned off is expected to be doing nothing:
        # `enabled` is read here rather than by the view, so a rail cannot go
        # amber because the operator switched something off.
        **wants(
            state,
            by_choice=not enabled,
            ended=ended,
            awaiting_operator=awaiting_operator,
        ),
        "said": said,
        "saidToModel": said if for_model is None else for_model,
        "at": _iso(at),
        "value": value,
        "opens": opens,
        "enabled": enabled,
        "canRun": can_run,
        "canToggle": can_toggle,
        "canDelete": can_delete,
        # Where the errors of a row that failed can be read, absolute and
        # resolved here. The path is the backend's for the reason every other
        # path on this panel is: `TESSERACT_HOME` moves per install, so a view
        # that composed one would be right on the machine it was written on and
        # wrong everywhere else. Empty on a row that did not fail, which is what
        # keeps the control off every other line.
        "logPath": _log_path(state),
    }


def _log_path(state: OperationalState) -> str:
    """The backend log, for a row whose own run went wrong.

    Running a job again is not finding out why it failed, and until this the
    room offered the first and had no way to offer the second. Blank rather
    than a guess when the file is not there yet: a control that opens nothing
    is worse than no control, and the row's sentence already says what
    happened.
    """
    from tesseract import paths

    if state not in (OperationalState.FAILED, OperationalState.DEGRADED):
        return ""
    log = paths.runtime_logs_root() / "mirror-backend.log"
    return log.resolve().as_posix() if log.is_file() else ""


def when_it_fires(
    job: Any,
    runtime: dict[str, Any] | None,
    now: datetime,
    last_fired_at: datetime | None = None,
) -> str:
    """When this row comes round again, in the operator's terms.

    A row fires on a clock or on an event and declares exactly one of the two,
    so this says whichever it declared. An event row has no next time to
    compute and the engine's own reason for it is the better answer.

    The last fire is an argument because an interval is measured from it and
    not from the clock, which `due_after_last` is where it is decided. Asking
    from `now` put a future time on a row the engine already held overdue.
    """
    fires_on = (runtime or {}).get("when") or getattr(job, "when", "")
    if fires_on:
        reason = (runtime or {}).get("when_reason") or ""
        return reason or f"waits for {fires_on}"
    cadence = (runtime or {}).get("cadence") or getattr(job, "cadence", "")
    if not cadence:
        return "nothing says when it fires"
    words = in_words(cadence)
    due = due_after_last(cadence, last_fired_at, now)
    # `in_words` hands back the expression untouched when it cannot phrase it,
    # and its own docstring says why: a cadence shown raw is honest. Hanging a
    # clause on it would turn cron syntax into a sentence, so it stands alone.
    if due is None or words == cadence:
        return words
    if due <= now:
        return f"{words}, due now"
    return f"{words}, next {when_words(due, now)}"


def schedule_line(
    job: Any,
    runtime: dict[str, Any] | None,
    row: dict[str, Any] | None,
    *,
    is_system: bool,
    can_delete: bool,
    now: datetime,
    running: bool = False,
) -> dict[str, Any]:
    """One scheduled row, as the log and the engine between them describe it.

    Five readings, in the order they override each other. A row firing RIGHT
    NOW is working, and that beats every record of what it did last: this panel
    exists to answer what is running, and a job eleven minutes into a long pass
    reading as its previous outcome answers a different question. A row the
    operator turned off expects nothing of itself, which is what `idle` means.
    A row that stopped itself after three failures did not start on purpose,
    which is a refusal. Otherwise the last run in the log decides, through the
    function that writes one.
    """
    runtime = runtime or {}
    enabled = bool(getattr(job, "enabled", True)) and bool(runtime.get("enabled", True))
    fired_at = _parse(runtime.get("last_fired_at")) or _parse(
        (row or {}).get("fired_at")
    )
    fires = when_it_fires(job, runtime, now, fired_at)

    aged_out = False
    # What a model is told instead, on the one reading where `said` is not
    # this runtime's own words. `None` everywhere else, meaning both readers
    # get the same line, which is true of the four readings composed here.
    for_model: str | None = None
    if running:
        state, said = OperationalState.RUNNING, "running now"
    elif not enabled:
        state, said = OperationalState.IDLE, "turned off"
    elif runtime.get("circuit_broken"):
        state = OperationalState.REFUSED
        failures = int(runtime.get("consecutive_failures") or 0)
        said = (
            f"stopped itself after {failures} failures in a row. "
            "Turn it off and on again to let it try"
        )
    elif row is not None:
        state = state_of(outcome_of_row(row))
        said = str(row.get("outcome_reason") or "") or fires
        # `outcome_reason` is the one string on this row the runtime did not
        # choose. `JobResult` documents it as free plain language and
        # `scheduler/engine.py` builds one branch of it as
        # `f"unhandled exception: {exc!r}"`, so it can carry a path, a URL or
        # a server's own words. The operator reads that; a model gets the
        # outcome, which is a closed vocabulary. Left as `said` it was relayed
        # verbatim by `autonomy_read`, which is `auto` and asks nobody.
        for_model = f"its last run ended {state_of(outcome_of_row(row)).value}"
    elif fired_at is not None:
        # It ran, and the log no longer has the record. A retention sweep aged
        # it out, or it fired before this install kept one. Either way the room
        # cannot say how it went, and saying it never ran would be a claim.
        # It ran and the record aged out, which is a fact with a time and not
        # something to act on. Without `ended` every machine that ever swept
        # its run log would carry an amber row for ever.
        state = OperationalState.UNKNOWN
        said = "it has run, and the run log no longer carries the record"
        aged_out = True
    else:
        state, said = OperationalState.IDLE, fires

    name = str(getattr(job, "name", ""))
    return line(
        name=name,
        origin=SYSTEM if is_system else USER,
        tag=tag_of(SYSTEM if is_system else USER),
        state=state,
        said=said,
        at=fired_at,
        # When it next comes round, beside what it last did. Blank when the
        # row has nothing to report yet and `said` IS that sentence: the same
        # words twice on one line reads as a rendering fault, and it was one.
        value="" if not enabled or said == fires else fires,
        for_model=for_model,
        # Everything declared in the manifest has a card. A row the operator
        # wrote has one too, built from their own schedule: the card route
        # answers for both, and a row with nowhere to go would be the only
        # thing on this panel that cannot say what it is for.
        opens={"kind": "entry", "id": name},
        enabled=enabled,
        ended=aged_out,
        # Not while it is already going. The engine would happily start a
        # second run of the same job, and offering the control is what makes
        # that the operator's accident rather than their decision.
        can_run=not running,
        can_toggle=True,
        # A shipped row is not the operator's to delete: it comes back on the
        # next update and the app would then be running something they removed.
        # Not `not is_system`, though the two agree in an install. In a dev
        # checkout every row IS shipped, and the tree holding them is also the
        # operator's own source, so the mark says "built-in" while the row
        # stays removable. One flag answering both questions is what made the
        # whole room read as the assistant's work.
        can_delete=can_delete,
    )


def firing_now(scheduler: Any = None) -> set[str]:
    """The jobs that are running, by name, from both things that know.

    **The engine is asked first, because it is what decides.** It claims a
    job's seat at the moment a door commits to running it and refuses a second
    run from that instant; the activity registry, which `GET /api/activity`
    reads, is written a little later, inside the run itself. Reading only the
    registry left a window where the engine would refuse a run that this panel
    was still offering, which is the same defect as the one this room fixed in
    the other direction, pointed the other way.

    So both are read and the answer is the union. They are not rivals: the
    engine knows what has been committed to, the registry knows what is
    executing, and a row is busy if either says so. The registry also carries
    routines the scheduler did not start, which is why it is not simply
    dropped.
    """
    names: set[str] = set()
    running = getattr(scheduler, "is_running", None)
    registry_of = getattr(scheduler, "registry", None)
    if running is not None and registry_of is not None:
        try:
            names |= {name for name in registry_of if running(name)}
        except Exception:  # noqa: BLE001 — a room says less rather than 500ing
            log.exception("managed route: the scheduler could not say what is running")

    from tesseract.orchestrator.activity.registry import get_activity_registry

    try:
        live = get_activity_registry().snapshot()
    except Exception:  # noqa: BLE001 — a room says less rather than 500ing
        log.exception("managed route: the activity registry could not be read")
        return names
    return names | {
        str(record.label)
        for record in live
        if getattr(record, "kind", "") == "routine"
        and getattr(record, "state", "") == "running"
        and getattr(record, "label", "")
    }


def schedules(app: web.Application, now: datetime) -> list[dict[str, Any]]:
    """Every scheduled row, the app's first and the operator's after."""
    from tesseract.scheduler.config_loader import shipped_job_names, system_job_names

    scheduler = app.get("scheduler")
    if scheduler is None:
        return []
    # Two questions, two answers. `shipped_job_names` is who wrote the row, and
    # in a dev checkout the one config tree is the shipped one so every row is
    # the app's. `system_job_names` is whether the row's fields are sealed
    # here, and it says nothing in that same checkout so the repo's own
    # schedule stays editable. They agree in an install and must not be folded.
    try:
        shipped = shipped_job_names(scheduler.config_dir)
        sealed = system_job_names(scheduler.config_dir)
    except Exception:  # noqa: BLE001 — a room says less rather than 500ing
        log.exception("managed route: the shipped schedule could not be read")
        shipped, sealed = set(), set()
    try:
        rows = last_run_rows()
    except OSError:
        log.warning("managed route: the run log could not be read")
        rows = {}
    live = firing_now(scheduler)

    out: list[dict[str, Any]] = []
    for cfg in scheduler.configs:
        try:
            runtime = scheduler.runtime_state(cfg.name)
        except KeyError:
            runtime = None
        out.append(
            schedule_line(
                cfg,
                runtime,
                rows.get(cfg.name),
                is_system=cfg.name in shipped,
                can_delete=cfg.name not in sealed,
                now=now,
                running=cfg.name in live,
            )
        )
    out.sort(key=lambda r: (r["origin"] != SYSTEM, r["name"]))
    return out


def agents() -> list[dict[str, Any]]:
    """Every agent card, active and waiting for approval.

    The prose is the card's own `description`, which is the same contract every
    entry on this panel holds to. An agent with none says so rather than being
    given one here.
    """
    from tesseract.agents.loader import (
        list_agents,
        list_agents_by_origin,
        list_pending_agents,
        load_agent,
    )

    out: list[dict[str, Any]] = []
    try:
        active, pending = list_agents(), list_pending_agents()
    except Exception:  # noqa: BLE001
        log.exception("managed route: the agent roster could not be read")
        return []
    # An active card shadows a pending one of the same name, which is what
    # `list_agents(include_pending=True)` already does and this route was not
    # doing: reading the two lists apart and joining them showed the name
    # twice, and `waiting` keyed on the name alone marked BOTH rows as
    # unapproved. The live card lost its off switch to a copy of itself.
    live = set(active)
    pending = [name for name in pending if name not in live]
    # Which root each card came from, resolved without opening it. A card that
    # will not parse still has an owner, and filing a broken SHIPPED one under
    # Yours would be wrong on the one axis this room exists to get right.
    try:
        by_origin = list_agents_by_origin()
    except Exception:  # noqa: BLE001
        log.exception("managed route: the agent roots could not be read")
        by_origin = {"system": [], "user": []}
    shipped = set(by_origin.get("system") or ())
    # Why a card cannot run, if it cannot. A shipped one in that state stops
    # the boot, so what reaches here is the operator's own — reported rather
    # than refused, which until now meant one log line the Mirror does not
    # forward and a row on this panel that looked like every healthy row.
    # The catalog is resolved ONCE for the whole render and each card is
    # judged from the copy this loop already opened.
    from tesseract.agents.contract import catalog_or_none, gaps_for_card

    try:
        catalog = catalog_or_none()
    except Exception:  # noqa: BLE001 — a panel that cannot judge still lists
        log.exception("managed route: the model catalog could not be read")
        catalog = None
    for name in [*active, *pending]:
        waiting = name in pending
        try:
            agent = load_agent(name, include_pending=True)
        except Exception as exc:  # noqa: BLE001
            out.append(
                line(
                    name=name,
                    origin=SYSTEM if name in shipped else USER,
                    tag=tag_of(SYSTEM if name in shipped else USER),
                    state=OperationalState.UNKNOWN,
                    said=f"its card is on disk and could not be read: {exc}",
                    # The row that most needs opening was the one row here that
                    # could not be: `opens=None` makes it unclickable, so an
                    # operator was told a card is broken and given no way to
                    # reach it. `schedule_line` says the rule this broke.
                    opens={"kind": "agent", "id": name},
                )
            )
            continue
        try:
            gaps = gaps_for_card(name, agent.origin, agent, bundle=catalog)
        except Exception:  # noqa: BLE001 — a panel that cannot judge still lists
            log.exception("managed route: %s could not be judged", name)
            gaps = []
        gap = next((g for g in gaps if g.blocking), None)
        if waiting:
            state = OperationalState.PENDING
            said = "waiting for you to approve it"
        elif gap is not None:
            # Both, when both are true. Turning a card off is something the
            # operator DID and the toggle beside this sentence still says so,
            # so a sentence that mentions only the fault contradicts the row
            # it sits on.
            state = OperationalState.UNKNOWN
            off = "It is also turned off. " if agent.disabled else ""
            said = f"{off}It cannot run as written: {gap.detail}"
        elif agent.disabled:
            state = OperationalState.IDLE
            said = "turned off"
        else:
            state = OperationalState.IDLE
            said = agent.description or "its card says nothing about what it is for"
        out.append(
            line(
                name=name,
                origin=agent.origin,
                tag=tag_of(agent.origin),
                state=state,
                said=said,
                value=agent.model_role,
                opens={"kind": "agent", "id": name},
                enabled=not agent.disabled,
                # `pending` asks for nothing when it is a stage inside an open
                # run, and is the whole of what this row asks: the card cannot
                # be used by anything until the operator has looked at it. The
                # row knows which of the two it is and the state does not.
                awaiting_operator=waiting,
                can_toggle=not waiting,
            )
        )
    out.sort(key=lambda r: (r["origin"] != SYSTEM, r["name"]))
    return out


def playbooks(app: web.Application) -> list[dict[str, Any]]:
    """Every playbook: a skill that has declared the procedure contract.

    Every playbook is the operator's. Skills have one root,
    `workspace_dir()/skills`, and it is the operator's in every install: no
    playbook ships, so nothing here is filed under the app.

    A retired revision is listed with the rest, status and all: this room is
    the record, and it is the prompt (`brain/prompt.py`'s skills block) that
    is the one place a retired playbook is not offered.
    """
    from tesseract import paths
    from tesseract.brain.playbook_contract import blocking_gaps
    from tesseract.brain.skills import load_skills

    try:
        entries = [
            entry
            for entry in load_skills(paths.workspace_dir() / "skills")
            if entry.is_playbook
        ]
    except Exception:  # noqa: BLE001 — a room says less rather than 500ing
        log.exception("managed route: the playbook roster could not be read")
        return []

    registry = app.get("tool_registry")
    tool_names = frozenset(registry.names()) if registry is not None else None
    gaps = blocking_gaps(entries, tool_names=tool_names)

    out: list[dict[str, Any]] = []
    for entry in entries:
        gap = gaps.get(entry.name)
        # The one state a playbook has here: it can run, or it cannot.
        # `refused` is the room's own word for a thing that did not start,
        # and it is said HERE, with its label, because a surface may not
        # decide a state a producer did not emit.
        state = OperationalState.REFUSED if gap is not None else OperationalState.IDLE
        out.append(
            {
                "name": entry.name,
                # Every playbook is the operator's own: nothing ships one.
                "origin": USER,
                "state": state.value,
                "label": label_of(state),
                # A playbook that cannot run is missing something it declared,
                # which is a thing to fix rather than a choice somebody made.
                **wants(state),
                "version": entry.version,
                "status": entry.status,
                "description": entry.description,
                "useWhen": entry.use_when,
                "trigger": entry.trigger,
                "steps": len(entry.steps),
                "cannotRun": f"{gap.field} {gap.detail}" if gap is not None else None,
                "path": f"{entry.dirname}/SKILL.md",
            }
        )
    out.sort(key=lambda r: r["name"])
    return out


def alarms(app: web.Application) -> list[dict[str, Any]]:
    """One-shot and recurring alarms, all of them the operator's.

    No row here carries the ownership mark, and that is the reason: nothing
    ships an alarm, so the answer is the same on every line and a mark that
    never varies is chrome the row pays width for.
    """
    registry = app.get("alarm_registry")
    if registry is None:
        return []
    try:
        pending = list(registry.list_pending())
    except Exception:  # noqa: BLE001
        log.exception("managed route: the alarms could not be read")
        return []
    out: list[dict[str, Any]] = []
    for alarm in pending:
        every = alarm.recurrence.to_dict() if alarm.recurrence else None
        out.append(
            line(
                name=alarm.label,
                origin=USER,
                # Something IS expected of it and it has not happened, which is
                # what separates a waiting alarm from a quiet one.
                state=OperationalState.PENDING,
                said=alarm.message or "it will say nothing, it will just go off",
                # No mark: see this function's own docstring.
                tag="",
                at=alarm.run_at,
                value="repeats" if every else "",
                can_delete=True,
            )
        )
    out.sort(key=lambda r: r["at"] or "")
    return out


def _or_empty(which: str, result: Any) -> Any:
    """A roster that raised is an empty roster; a cancellation is not.

    `Exception` and not `BaseException`, because `return_exceptions=True` hands
    a cancelled child back as a result: catching the wider class turned a
    request the caller had abandoned into a room with nothing in it.
    """
    if isinstance(result, Exception):
        log.exception("managed route: the %s roster raised", which, exc_info=result)
        return []
    if isinstance(result, BaseException):
        raise result
    return result


async def get_managed(request: web.Request) -> web.Response:
    """The four rosters, each a list with who wrote a row marked on it."""
    now = datetime.now(timezone.utc)
    # Reading the schedule walks the run log and reading the agents parses
    # every card, and the loop this rides carries health, the socket and every
    # inbound turn.
    # One roster failing is not the others failing, and all four answer the
    # same way. Every reader inside these functions already catches its own
    # faults so the room says less rather than 500ing, and the code BETWEEN
    # those catches does not: `alarms` guards the read of the registry and not
    # the loop that turns each alarm into a row, so one malformed alarm took
    # the whole room down while the rosters beside it were fine. A fourth
    # bespoke guard is what this replaces.
    rows, roster, bells, plays = await asyncio.gather(
        asyncio.to_thread(schedules, request.app, now),
        asyncio.to_thread(agents),
        asyncio.to_thread(alarms, request.app),
        asyncio.to_thread(playbooks, request.app),
        return_exceptions=True,
    )
    return web.json_response(
        {
            "schedules": _or_empty("schedule", rows),
            "agents": _or_empty("agent", roster),
            "alarms": _or_empty("alarm", bells),
            "playbooks": _or_empty("playbook", plays),
            "observedAt": _iso(now),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/managed", get_managed)


__all__ = [
    "SYSTEM",
    "USER",
    "agents",
    "tag_of",
    "firing_now",
    "alarms",
    "get_managed",
    "line",
    "playbooks",
    "register",
    "schedule_line",
    "schedules",
    "when_it_fires",
]
