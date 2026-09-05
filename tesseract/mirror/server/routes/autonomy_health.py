"""Is the runtime itself well, in one feed.

``GET /api/autonomy/health`` answers the question the Autonomy panel's Health
room is for, and it answers it from what the machine has already written down
rather than by asking the machine again. The watchman looks at ten sources and
the machine itself every quarter of an hour and leaves
``autonomy/watchman/latest.json`` behind; that file already carries every
source's PRESENCE and not only its findings, for exactly this reason, and its
own docstring says so.

So this route reads a small JSON file, the recovery summary the app is already
holding, and two directories. It is cheap enough to poll.

**Three bands, and which one a department lands in is derived from its state,
never declared twice.** Something that failed or is working below what it
promised needs the operator. Something nothing reports on is blind. Everything
else is operating. The one rule that governs all of it is the phase's: a
department whose producer does not exist renders as `not_instrumented` and
never as quiet, because a room that goes silent when its camera is unplugged
says nothing is wrong.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

from aiohttp import web

from tesseract import paths
from tesseract.lib.log_envelope import BAD, INFO, WARN
from tesseract.mirror.server.routes._isotime import iso as _iso
from tesseract.mirror.server.routes._isotime import parse as _parse
from tesseract.orchestrator.liveness import OperationalState, label_of

log = logging.getLogger(__name__)

# How many lines of the runtime's own errors sit under the findings. Evidence
# beneath the room, not a feed competing with it.
TAIL_LINES = 4

# How far back the tail looks, and how much of a log file it opens. A boot loop
# writes a lot, and an error only visible in the first megabyte of a two
# hundred megabyte log is not the one anybody is looking for.
TAIL_HOURS = 24
# Four lines out of the end of a log. 128KB per file was a boot log's whole
# tail; this is the last few hundred lines of each, which is where an error
# from the last day is.
TAIL_BYTES = 32 * 1024
# How many log files back to read. One process writes one file per boot and
# more than one process writes into this directory, so the newest file alone is
# whichever booted last rather than wherever the error is.
LOG_FILES = 6

# A sweep older than this is not a reading of now. The watchman fires every
# quarter of an hour, so an hour is four missed turns: past that the panel says
# it does not know rather than showing a stale green.
SWEEP_STALE_AFTER = timedelta(hours=1)

# How far back the event-loop row looks. A stall is an event, not a state, so
# the room needs a window or the newest one on file stands as the answer
# forever: a machine that blocked once a fortnight ago would read as blocked
# until somebody deleted the record.
STALL_WINDOW = timedelta(hours=24)

# What each band is called, and the order they are read in.
NEEDS_ACTION = "needs_action"
BLIND = "blind"
OPERATING = "operating"

_BAND_OF: dict[OperationalState, str] = {
    OperationalState.FAILED: NEEDS_ACTION,
    OperationalState.DEGRADED: NEEDS_ACTION,
    OperationalState.REFUSED: NEEDS_ACTION,
    OperationalState.NOT_INSTRUMENTED: BLIND,
    OperationalState.UNKNOWN: BLIND,
    OperationalState.RUNNING: OPERATING,
    OperationalState.IDLE: OPERATING,
    OperationalState.PENDING: OPERATING,
}

_STATE_OF_SEVERITY: dict[str, OperationalState] = {
    BAD: OperationalState.FAILED,
    WARN: OperationalState.DEGRADED,
    INFO: OperationalState.IDLE,
}

# What each source the watchman reads is called on a screen. The collector
# names are file-tree names; a person reading a room needs the thing itself.
_SOURCE_NAMES: dict[str, str] = {
    "circuit-breakers": "circuit breakers",
    "supervisor": "the supervisor",
    "backend": "the backend log",
    "janitor": "the janitor",
    "governor": "the governor",
    "workers": "workers",
    "provider-health": "providers",
    "conscience": "the conscience",
    "schedule": "scheduled work",
    # Named even though `_HAS_ITS_OWN_READER` keeps the sweep's copy of it off
    # the screen. This map is total over the collectors by test, and a name
    # that is here is a name the room has the day that set is emptied.
    "loop-stalls": "how long the app was blocked",
    "diagnostics": "the machine itself",
    "repairs": "what it fixed itself",
}


# Sources whose fact already has a reader of its own in this room, so the
# sweep's copy of it is not drawn. Rule 4 is the reason: no fact gets two
# readers, and a watchman finding and a live reader are two answers to one
# question that can disagree about the same day. The live reader wins because
# it is fresher than a quarter-hour sweep and because it still says something
# where the judge deliberately drops the finding, which for a stall is every
# block that began and ended inside a boot. The sweep's copy still reaches the
# report and the brief, which is where a finding is what was wanted.
_HAS_ITS_OWN_READER = frozenset({"loop-stalls"})


def department(
    *,
    name: str,
    state: OperationalState,
    said: str,
    at: datetime | None = None,
    value: str = "",
    for_model: str | None = None,
) -> dict[str, Any]:
    """One line in a Health band.

    `label` travels with the state because `not_instrumented` is unreadable to
    anybody who has not read the code, and the translation belongs where the
    state is defined rather than in TSX.

    There is deliberately no `opens`. A department is not a thing the panel can
    open: the level stack takes a kind and an id, a finding has neither, and a
    field naming a contract nothing can honour is worse than the absence of
    one. When Health rows do navigate, they will publish a real target.
    """
    return {
        "name": name,
        "band": _BAND_OF[state],
        "state": state.value,
        "label": label_of(state),
        "said": said,
        # The same department in the words that may leave this machine, for
        # the room line a model writes. `said` is the operator's copy and may
        # carry a log line or a provider's own text; `saidToModel` carries
        # only what this runtime composed. Empty means there is nothing safe
        # to forward, and the caller says less rather than guessing.
        "saidToModel": said if for_model is None else for_model,
        "at": _iso(at),
        "value": value,
    }


def latest_path() -> Path:
    """Resolved at call time — an import-time constant freezes the path before
    a relocated home is known."""
    return paths.home_dir() / "autonomy" / "watchman" / "latest.json"


# What a sweep read gives back. `None` means the file is not there and the
# watchman has never run here; a string means it IS there and could not be
# used. Those are different claims about the same room, and collapsing them
# reports a read failure as a producer that does not exist, which is the exact
# confusion this panel was built to end.
Sweep = dict[str, Any] | str | None


def read_latest() -> Sweep:
    """The watchman's last sweep, why it could not be read, or None."""
    path = latest_path()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("health route: could not read the sweep at %s", path)
        return f"the last sweep could not be read: {exc.strerror or exc}"
    except ValueError:
        log.warning("health route: unparseable sweep at %s", path)
        return "the last sweep is on disk and is not readable JSON"
    if not isinstance(loaded, dict):
        return "the last sweep is on disk and is not the shape a sweep has"
    return loaded


def from_sweep(latest: Sweep, now: datetime) -> list[dict[str, Any]]:
    """Every department the watchman can speak for.

    A sweep that has never run is not an empty sweep: nothing has looked, so
    every department it would have covered is unknown rather than quiet. That
    is the whole distinction this room exists to draw. A sweep that exists and
    could not be read is a third answer again, and the one most worth getting
    right: something IS watching and this room cannot see what it saw.
    """
    if latest is None:
        return [
            department(
                name="the watchman",
                state=OperationalState.NOT_INSTRUMENTED,
                said="it has not run on this machine yet, so nothing has looked at the runtime",
            )
        ]
    if isinstance(latest, str):
        return [
            department(
                name="the watchman",
                state=OperationalState.UNKNOWN,
                said=latest,
                # It ends in an OS error string, which is text this runtime was
                # handed however carefully the sentence around it was written.
                for_model="the last sweep is on disk and could not be read",
            )
        ]

    observed = _parse(latest.get("observed_at"))
    stale = observed is None or now - observed > SWEEP_STALE_AFTER

    out: list[dict[str, Any]] = []

    # What the operator has already looked at and left. Read once for the
    # whole sweep rather than per finding, and pruned to what this sweep
    # actually found: the condition ending is what ends the acknowledgement,
    # so a decision about today's incident cannot silently pre-accept the same
    # fault arriving next month.
    seen = _acknowledged(latest)

    # What it found. One line per finding, named by its subject, because that
    # is what a person acts on: the ref that is failing, the row that did not
    # fire. The words are the finding's own.
    for finding in latest.get("findings") or []:
        severity = str(finding.get("severity") or INFO)
        source = str(finding.get("source") or "")
        if source in _HAS_ITS_OWN_READER:
            continue
        # An acknowledged finding is still drawn, and drawn with its own
        # sentence. It moves to Operating and says who left it there, because
        # the operator asked to stop being alarmed by it and not to stop being
        # told about it. It returns on its own if it gets worse than it was
        # when they looked.
        held = seen.get(_key_of(finding))
        answered = held is not None and held.covers(severity)
        said = str(finding.get("summary") or "")
        if answered and held is not None:
            said = _left_alone(said, held)
        out.append(
            department(
                name=str(finding.get("subject") or _SOURCE_NAMES.get(source, source)),
                state=(
                    OperationalState.IDLE
                    if answered
                    else _STATE_OF_SEVERITY.get(severity, OperationalState.DEGRADED)
                ),
                said=said,
                # The watchman's own gate, read rather than re-derived: a
                # finding whose summary carries text the runtime was handed
                # writes a second one that does not. A sweep from before that
                # key existed carries nothing here, and nothing is what gets
                # forwarded.
                for_model=str(finding.get("model_summary") or ""),
                at=_parse(finding.get("last_at")),
                value=str(finding.get("count") or ""),
            )
        )

    # Where it could not look. A source that is absent and one that errored are
    # different claims and read differently: nothing produces this, against
    # something produces it and could not be read.
    for source in latest.get("sources") or []:
        if str(source.get("name")) in _HAS_ITS_OWN_READER:
            continue
        name = _SOURCE_NAMES.get(str(source.get("name")), str(source.get("name")))
        if not source.get("present"):
            out.append(
                department(
                    name=name,
                    state=OperationalState.NOT_INSTRUMENTED,
                    said="nothing on this machine writes this yet",
                )
            )
            continue
        if source.get("error"):
            out.append(
                department(
                    name=name,
                    state=OperationalState.UNKNOWN,
                    said=str(source["error"]),
                    # A reader's own error, which is whatever the OS or a
                    # parser said. The count is the part that travels.
                    for_model="",
                    at=observed,
                )
            )
            continue
        if source.get("findings"):
            # Already on the list above, under the name of what it found.
            continue
        scanned = int(source.get("scanned") or 0)
        out.append(
            department(
                name=name,
                state=OperationalState.UNKNOWN if stale else OperationalState.IDLE,
                said=(
                    "the last look at it is too old to stand for now"
                    if stale
                    else "read, nothing to report"
                ),
                at=observed,
                value=str(scanned) if scanned else "",
            )
        )

    # A stage that could not read its own evidence is part of what the room has
    # to show, or the panel is as blind as the report was.
    for spot in latest.get("blind_spots") or []:
        out.append(
            department(
                name=str(spot),
                state=OperationalState.NOT_INSTRUMENTED,
                said="the judge could not read what it needed to decide this",
            )
        )
    return out


def _key_of(finding: dict[str, Any]) -> str:
    """A finding's identity, in the words the judge already uses for it.

    `standing.key_for` is what decides whether two sweeps saw the same fault,
    and an acknowledgement keyed any other way would drift from it the first
    time a subject was renamed. It takes an object rather than a mapping, so
    this restates the join and a test holds the two together.
    """
    return "{}/{}/{}".format(
        finding.get("source") or "",
        finding.get("kind") or "",
        finding.get("subject") or "",
    )


def _left_alone(said: str, held: Any) -> str:
    """The finding's own sentence, plus who left it and when.

    Never instead of it. What is true stays on the row; this adds why it is
    not shouting.
    """
    when = held.at.date().isoformat()
    tail = f"You marked this as seen on {when}."
    if held.note:
        tail += f" {held.note}"
    return f"{said} {tail}".strip()


def _acknowledged(latest: Sweep) -> dict[str, Any]:
    """What the operator has left alone, narrowed to this sweep's findings.

    Pruning here rather than on a schedule keeps the store honest without a
    job to run: the room reads the sweep every minute, and the sweep is the
    only thing that knows which faults are still live.
    """
    from tesseract.orchestrator.watchman import acknowledged

    if not isinstance(latest, dict):
        return {}
    live = {_key_of(f) for f in (latest.get("findings") or []) if isinstance(f, dict)}
    try:
        acknowledged.prune(live)
        return acknowledged.load()
    except Exception:  # noqa: BLE001 — a room says more rather than failing
        log.exception("health route: the acknowledged store could not be read")
        return {}


def judge_stages(latest: Sweep) -> list[dict[str, Any]]:
    """The six steps, as counts.

    A judge nobody can inspect is a filter, and a filter that silently drops a
    real fault is the failure this phase exists to prevent. Every verdict is in
    the record, not only the surviving ones, so the counts are a reading of it
    rather than a second derivation.
    """
    if not isinstance(latest, dict):
        return []
    order: list[str] = []
    kept: dict[str, int] = {}
    dropped: dict[str, int] = {}
    for verdict in latest.get("judgement") or []:
        stage = str(verdict.get("stage") or "")
        if stage not in kept:
            order.append(stage)
            kept[stage] = 0
            dropped[stage] = 0
        if str(verdict.get("action")) == "kept":
            kept[stage] += 1
        else:
            dropped[stage] += 1
    return [
        {"stage": stage, "kept": kept[stage], "dropped": dropped[stage]}
        for stage in order
    ]


def from_recovery(app: web.Application) -> list[dict[str, Any]]:
    """What the last boot reconciled."""
    summary = app.get("last_recovery_summary")
    if summary is None:
        return [
            department(
                name="recovery",
                state=OperationalState.IDLE,
                said="nothing needed reconciling at the last boot",
            )
        ]
    try:
        payload = summary.to_payload()
    except Exception:  # noqa: BLE001 — reported as unknown, never 500 the room
        log.exception("health route: the recovery summary could not be read")
        return [
            department(
                name="recovery",
                state=OperationalState.UNKNOWN,
                said="the last pass left a summary this route could not read",
            )
        ]
    scans = payload.get("scans") or {}
    touched = sum(int(v) for v in scans.values() if isinstance(v, (int, float)))
    waiting = len(payload.get("operator_attention") or [])
    started = getattr(summary, "started_at", None)
    return [
        department(
            name="recovery",
            state=OperationalState.DEGRADED if waiting else OperationalState.IDLE,
            said=(
                f"{waiting} thing{'' if waiting == 1 else 's'} came out of the last boot "
                "needing you"
                if waiting
                else f"reconciled {touched} record{'' if touched == 1 else 's'} at the last boot"
            ),
            at=started if isinstance(started, datetime) else None,
            value=str(touched) if touched else "",
        )
    ]


# What Health owes section 3 of the open book, and the properties every one of
# these readers holds AT ONCE. Checking a later edit against the one property in
# front of it is how this room drops a guarantee it already had.
#
# 1. Nothing here keeps a browser waiting. Every reader opens a file the machine
#    has already written. None of them probes hardware, reaches the network, or
#    spends anything, so none may call a reconciler or a prober.
# 2. A fact with no producer renders `not_instrumented` and says what that costs.
#    Silence would read as health.
# 3. A producer that EXISTS and could not be read is `unknown`, which is a
#    different claim, and keeping them apart is what this room is for.
# 4. No fact gets two readers. Each function below calls the module that owns
#    the fact, and derives nothing the owner already decided.
# 5. `for_model` carries only words this runtime composed. Text it was handed
#    stays in `said`, which is the operator's copy and never leaves the machine.
# 6. A reader that raises is one department, never a dead room. Every reader
#    in `get_health`'s gather goes through `_departments` or `_or_empty`, so
#    nothing in it can raise and take the room with it.
# 7. Which band a department lands in is derived from its state by `_BAND_OF`
#    and never declared a second time.


def from_capabilities() -> list[dict[str, Any]]:
    """What this machine cannot do, and what that costs.

    The launch pass already wrote its conclusions down. This reads that
    artifact and never reconciles: a room polling this must not be able to
    start a hardware probe, which is the same rule the Settings feed follows.

    `read_state` is total on purpose and answers `None` for absent, unreadable
    and malformed alike, because its own callers want one answer for all
    three. This room wants two, so the file's presence is asked separately.
    """
    from tesseract.capability.state import read_state, state_path

    # `exists` swallows only the errnos that MEAN absent. A directory held by
    # a scanner or refused by an ACL raises out of it, and reading that as
    # absent is the collapse this room exists to prevent: it would say nothing
    # has ever looked, on a machine where something has.
    try:
        present = state_path().exists()
    except OSError:
        return [
            department(
                name="capabilities",
                state=OperationalState.UNKNOWN,
                said=(
                    "whether this machine has been checked could not be read. "
                    "Run the check again from Settings"
                ),
            )
        ]
    if not present:
        return [
            department(
                name="capabilities",
                state=OperationalState.NOT_INSTRUMENTED,
                said=(
                    "nothing has checked what this machine can do yet, so this room "
                    "cannot say what is switched off. Run the check from Settings"
                ),
            )
        ]
    state = read_state()
    if state is None:
        return [
            department(
                name="capabilities",
                state=OperationalState.UNKNOWN,
                said=(
                    "the record of what this machine can do is on disk and could not "
                    "be read. Run the check again from Settings"
                ),
            )
        ]
    checked = _parse(state.checked_at)
    wanted = state.attention
    if not wanted:
        return [
            department(
                name="capabilities",
                state=OperationalState.IDLE,
                said="everything the app depends on is here",
                at=checked,
                value=str(len(state.dependencies)),
            )
        ]
    # Both kinds of attention are the same answer to the operator: a capability
    # is off the board and the rest of the app is still running. Grading one
    # above the other would be a severity this room invented, and the record
    # does not carry one.
    return [
        department(
            name=record.id,
            state=OperationalState.DEGRADED,
            said=record.reason or f"it is {record.state.value} and the app expects it",
            at=checked,
            value=f"{record.size_mb} MB" if record.size_mb else "",
        )
        for record in wanted
    ]


def from_crash_storm() -> list[dict[str, Any]]:
    """Whether the app has stopped so often that nothing will restart it.

    The supervisor writes one marker and exits when it does, and refuses to
    start again while the marker is there. That is the loudest thing this
    runtime can say about itself and it reached no surface at all.
    """
    from tesseract.supervisor.breaker import crash_storm_path

    name = "crash storm"
    try:
        raw = crash_storm_path(paths.home_dir()).read_text(encoding="utf-8")
    except FileNotFoundError:
        return [
            department(
                name=name,
                state=OperationalState.IDLE,
                said="the app has not stopped repeatedly, so it still restarts itself",
            )
        ]
    except OSError as exc:
        log.warning("health route: the crash storm marker could not be read")
        return [
            department(
                name=name,
                state=OperationalState.UNKNOWN,
                said=f"a marker is there and could not be read: {exc.strerror or exc}",
                for_model="a crash storm marker is on disk and could not be read",
            )
        ]
    latched: datetime | None = None
    crashes = 0
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        latched = _parse(payload.get("latched_at"))
        listed = payload.get("crashes")
        crashes = len(listed) if isinstance(listed, list) else 0
    return [
        department(
            name=name,
            state=OperationalState.FAILED,
            said=(
                "the app stopped too many times in a few minutes and nothing will "
                "start it again on its own. Clear it with "
                "python -m tesseract.scripts.clear_crash_storm"
            ),
            at=latched,
            value=str(crashes) if crashes else "",
        )
    ]


def from_retention() -> list[dict[str, Any]]:
    """What the machine threw away last night, in one line.

    The room that holds this per tree is Thrown away, and this is the line
    that says whether to open it. It reads the sweep's OWN record and nothing
    else: the sizes in that room come from walking two log trees, about a
    second on the machine this was built on, and a room polled every minute
    cannot pay for one.

    So the size the reference draws beside this line is not here. It is not
    in the held reading either: that cache carries each tree's size already
    formatted into words, so a total would be a second walk however it was
    reached. The number beside this line is what the sweep CLEARED, which the
    record already counted, costs nothing, and answers the question the line
    asks. The measured size stays in the room that pays for the walk.
    """
    from tesseract.retention import record

    name = "retention"
    last = record.read()
    if last is None:
        return [
            department(
                name=name,
                state=OperationalState.NOT_INSTRUMENTED,
                said=(
                    "no sweep has run on this machine yet, so nothing is being aged. "
                    "Thrown away has the windows that would apply tonight"
                ),
            )
        ]
    if isinstance(last, str):
        return [
            department(
                name=name,
                state=OperationalState.UNKNOWN,
                said=last,
                for_model="the last sweep's record could not be read",
            )
        ]
    trees = last.get("trees")
    trees = trees if isinstance(trees, dict) else {}
    counts = [c for c in trees.values() if isinstance(c, dict)]
    removed = sum(int(c.get("removed") or 0) for c in counts)
    moved = sum(int(c.get("moved") or 0) for c in counts)
    held = sum(int(c.get("held") or 0) for c in counts)
    failed = sum(int(c.get("failed") or 0) for c in counts)
    errors = last.get("errors")
    errors = len(errors) if isinstance(errors, list) else 0
    touched = removed + moved
    said = (
        f"the last sweep cleared {touched} of them"
        if touched
        else "the last sweep found nothing old enough to touch"
    )
    if held:
        said += f" and kept {held} back"
    if failed or errors:
        said += f", and could not touch {failed + errors}"
    return [
        department(
            name=name,
            # A tree it could not touch is the one thing here that wants
            # somebody. Nothing to delete is the commonest healthy night.
            state=OperationalState.DEGRADED
            if (failed or errors)
            else OperationalState.IDLE,
            said=f"{len(trees)} trees have a window and {said}",
            at=_parse(last.get("observed_at")),
            # What it cleared, not how many trees: the sentence already says
            # the tree count, and a number that restates its own line is a
            # column the operator learns to skip.
            value=str(touched) if touched else "",
        )
    ]


def from_loop_lag(now: datetime | None = None) -> list[dict[str, Any]]:
    """How long the app was blocked for, and what it was in at the time.

    A stall used to end in a log warning and nothing else, so a slow turn
    could not be told from a stuck one and there was nothing to compare with
    yesterday. There is a record now, one row per stall.

    Read over a window rather than as the last row on file. This room answers
    whether the runtime is well NOW, and the newest stall on a machine that
    has been fine for a fortnight is a fortnight old: reporting it as the
    state would leave the room red until somebody deleted the record.

    A directory that does not exist is the honest blind case and keeps saying
    so. The app having never stalled and nothing ever having looked are the
    same empty folder, and this room's one rule is not to render those alike.
    """
    from tesseract.orchestrator import loop_stalls

    name = "event loop"
    if not loop_stalls.root().is_dir():
        return [
            department(
                name=name,
                state=OperationalState.NOT_INSTRUMENTED,
                said=(
                    "nothing has been written here yet, so there is no block to "
                    "report and no way to tell that from never having looked"
                ),
            )
        ]
    end = now or datetime.now(timezone.utc)
    recent = loop_stalls.read_since(end - STALL_WINDOW, end)
    if not recent:
        last = loop_stalls.latest()
        at = _parse(last.get("ts")) if last else None
        said = (
            "the app has not been blocked long enough to record it"
            if last is None
            else "the app has not been blocked in the last day"
        )
        return [department(name=name, state=OperationalState.IDLE, said=said, at=at)]
    worst = max(recent, key=lambda r: float(r.get("seconds") or 0.0))
    seconds = float(worst.get("seconds") or 0.0)
    doing = str(worst.get("doing") or "").strip()
    counted = f"{len(recent)} time" if len(recent) == 1 else f"{len(recent)} times"
    plain = f"blocked {counted} in the last day, the longest for {seconds:.1f} seconds"
    said = f"{plain}, in {doing}" if doing else plain
    return [
        department(
            name=name,
            # The writer graded each row against the point where a block costs
            # the supervisor a heartbeat, which is where a slowdown becomes the
            # thing that gets the app killed and restarted. Read that word
            # rather than deciding again from the number.
            state=(
                OperationalState.FAILED
                if any(r.get("severity") == BAD for r in recent)
                else OperationalState.DEGRADED
            ),
            said=said,
            # The frames are this process's own stacks and stay the operator's
            # copy. What travels is the count and the number, which is what a
            # room line needs and carries nothing a model could be steered by.
            for_model=plain,
            at=_parse(worst.get("ts")),
            value=f"{seconds:.1f}s",
        )
    ]


# How much of the report the room will carry. Every one this machine has
# written is under 6KB, and the ceiling is here so a boot loop that files a
# thousand findings cannot put a log-sized document through a JSON response.
REPORT_BYTES = 64 * 1024


def read_report(latest: Sweep) -> dict[str, Any]:
    """The report the brief sends, in full, as the operator's own copy.

    The path comes off the sweep's own record rather than by looking in the
    directory for the newest file. Two answers to "which report is the current
    one" is how a room comes to show yesterday's while the rail counts today's.

    It is the operator's copy and stays that way: it quotes whatever text the
    runtime was handed, so nothing here is offered to the narration model.
    """
    if latest is None:
        return {"state": "none", "said": "no report has been written on this machine yet"}
    if isinstance(latest, str):
        return {"state": "unknown", "said": "the last sweep could not be read, so its report was not opened"}
    raw_path = str(latest.get("summary_path") or "")
    if not raw_path:
        return {"state": "none", "said": "the last sweep did not record where its report was written"}
    path = Path(raw_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {
            "state": "none",
            "path": raw_path,
            "said": "the last sweep recorded a report that is no longer on disk",
        }
    except OSError as exc:
        log.warning("health route: the watchman report at %s could not be read", path)
        return {
            "state": "unknown",
            "path": raw_path,
            "said": f"the report is on disk and could not be read: {exc.strerror or exc}",
        }
    cut = len(text) > REPORT_BYTES
    return {
        "state": "read",
        "path": raw_path,
        "text": text[:REPORT_BYTES],
        "cut": cut,
        # Whether a model wrote the sentence at the top of it. A report with no
        # narration is the counted lines alone, which is worth knowing before
        # reading it as prose.
        "narrated": bool(latest.get("narrated")),
    }


def diagnostics_dir() -> Path:
    return paths.runtime_dir() / "diagnostics"


def from_stack_dumps(now: datetime) -> list[dict[str, Any]]:
    """A stack dump is the loop having been blocked long enough to be worth
    photographing. One today is worth saying out loud."""
    directory = diagnostics_dir()
    if not directory.is_dir():
        return [
            department(
                name="stack dumps",
                state=OperationalState.NOT_INSTRUMENTED,
                said="nothing on this machine has taken one",
            )
        ]
    # One stat per file, kept, rather than three calls that can disagree. A
    # dump swept between the listing and the stat used to raise out of the
    # gather in `get_health` and take the whole room down with it, which is a
    # 500 on a panel whose job is to say what is wrong.
    stamped: list[datetime] = []
    try:
        for path in directory.glob("stack-dump-*.json"):
            try:
                stamped.append(
                    datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                )
            except OSError:
                continue
    except OSError:
        return [
            department(
                name="stack dumps",
                state=OperationalState.UNKNOWN,
                said="the diagnostics directory could not be listed",
            )
        ]
    stamped.sort()
    dumps = stamped
    cutoff = now - timedelta(hours=TAIL_HOURS)
    recent = [at for at in stamped if at >= cutoff]
    last = stamped[-1] if stamped else None
    return [
        department(
            name="stack dumps",
            state=OperationalState.DEGRADED if recent else OperationalState.IDLE,
            # Says what happened to the operator and what it means for them.
            # It used to say "each one the loop blocked long enough to
            # photograph", which describes our mechanism, and inherited the
            # shared degraded label "working, below what it promised", which is
            # written for something missing a cadence and reads backwards on a
            # count where zero is the goal. The row has to stand on its own,
            # because the label it borrows cannot be right for both.
            said=(
                (
                    f"the app stopped answering {len(recent)} "
                    f"time{'s' if len(recent) != 1 else ''} in the last day, "
                    "for long enough that a snapshot was taken of what it was "
                    "stuck on. Anything you asked for around then may have "
                    "been slow. There is nothing to do unless it keeps "
                    "happening"
                )
                if recent
                else "nothing stopped answering in the last day"
            ),
            at=last,
            value=str(len(recent) or len(dumps)),
        )
    ]


def _line_stamp(line: str) -> datetime | None:
    """`2026-08-24 21:03:14,594` at the head of a log line, or None.

    The comma is `logging`'s millisecond separator and `fromisoformat` will not
    read one, which is the whole reason this is a function rather than a slice.

    **And the clock is the HOST's.** `logging.Formatter` renders `asctime`
    through `time.localtime` unless a converter says otherwise, and
    `logsetup.py` sets none. Reading it as UTC shifted every line in the tail
    by the machine's offset and moved the day-long window with it.
    """
    head = line[:23].strip()
    if len(head) < 19:
        return None
    parsed = _parse(head.replace(" ", "T", 1).replace(",", "."))
    if parsed is None:
        return None
    # `astimezone` on a naive stamp reads it in the offset in force NOW, not
    # the one in force when the line was written, so a line either side of a
    # daylight saving change is an hour out twice a year. That is the accuracy
    # the standard library gives without a tz lookup per line, and it is a long
    # way better than the offset-sized error every day that reading these as
    # UTC produced. The real fix is upstream: a formatter that emits UTC.
    return parsed.replace(tzinfo=None).astimezone()


def _log_tail(path: Path, limit: int = TAIL_BYTES) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
                handle.readline()
            return handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def runtime_tail(now: datetime) -> list[dict[str, Any]]:
    """The runtime's own errors as they happened, under the findings.

    The same lines the report sends, read from where the backend writes them.
    Evidence for what is above it: a room that states a finding and cannot show
    the line behind it is asking to be believed.
    """
    directory = paths.log_dir("backend")
    if not directory.is_dir():
        return []
    cutoff = now - timedelta(hours=TAIL_HOURS)
    try:
        logs = [
            p
            for p in sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime)
            if datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= cutoff
        ][-LOG_FILES:]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    # Where the line sat in the file, so two lines written in the same second
    # still come out in the order they were written. Newest first means the
    # later line, and a stable sort on the stamp alone gives the earlier one.
    seen = 0
    for path in logs:
        for line in _log_tail(path):
            level = BAD if " CRITICAL " in line else WARN if " ERROR " in line else ""
            if not level:
                continue
            stamp = _line_stamp(line)
            if stamp is not None and stamp < cutoff:
                continue
            # `2026-08-24 21:03:14,594 ERROR tesseract.brain.chat: it failed`
            rest = line.split(" ", 2)[-1] if stamp else line
            logger, _, text = rest.partition(":")
            seen += 1
            out.append(
                {
                    "at": _iso(stamp),
                    "sortKey": _iso(stamp) or "",
                    "source": logger.split(".")[-1].strip()[:24] or "backend",
                    "text": (text or rest).strip()[:200],
                    "severity": level,
                    "_at": seen,
                }
            )
    out.sort(key=lambda row: (row["sortKey"], row["_at"]), reverse=True)
    return [{k: v for k, v in row.items() if k != "_at"} for row in out[:TAIL_LINES]]


async def _departments(
    name: str, read: Callable[[], list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """One reader, off the loop, unable to take the room down with it.

    A reader that raises is a department saying it could not be read. A room
    whose job is to say what is wrong must not answer 500 because one of the
    files it opens was swept between the listing and the read.
    """
    try:
        return await asyncio.to_thread(read)
    except Exception:  # noqa: BLE001 — reported as a department, never a 500
        log.exception("health route: %s could not be read", name)
        return [
            department(
                name=name,
                state=OperationalState.UNKNOWN,
                said="this could not be read, so the room is not saying anything about it",
            )
        ]


async def _or_empty(name: str, read: Callable[[], Any], fallback: Any) -> Any:
    """The same guard for the two readers that do not answer in departments.

    Both are total by construction today. The guarantee is written here rather
    than left to that staying true, because the next reader added to the
    gather inherits it instead of taking the room down the first time a file
    is swept between the listing and the read.
    """
    try:
        return await asyncio.to_thread(read)
    except Exception:  # noqa: BLE001 — the room reports, never 500s
        log.exception("health route: %s could not be read", name)
        return fallback


async def get_health(request: web.Request) -> web.Response:
    """Every department, banded, with the runtime's own errors under them."""
    now = datetime.now(timezone.utc)
    # File reads on the loop that carries health, the socket and inbound turns.
    latest, dumps, tail, capabilities, storm, swept, stalls = await asyncio.gather(
        _or_empty("the last sweep", read_latest, "the last sweep could not be read"),
        _departments("stack dumps", lambda: from_stack_dumps(now)),
        _or_empty("the runtime log", lambda: runtime_tail(now), []),
        _departments("capabilities", from_capabilities),
        _departments("crash storm", from_crash_storm),
        _departments("retention", from_retention),
        _departments("event loop", lambda: from_loop_lag(now)),
    )
    departments = [
        *from_sweep(latest, now),
        *from_recovery(request.app),
        *dumps,
        *capabilities,
        *storm,
        *swept,
        *stalls,
    ]
    report = await asyncio.to_thread(read_report, latest)
    return web.json_response(
        {
            "departments": departments,
            "judge": judge_stages(latest),
            "report": report,
            "tail": tail,
            "sweptAt": _iso(
                _parse(latest.get("observed_at")) if isinstance(latest, dict) else None
            ),
            "observedAt": _iso(now),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/health", get_health)


__all__ = [
    "BLIND",
    "NEEDS_ACTION",
    "OPERATING",
    "department",
    "from_capabilities",
    "from_crash_storm",
    "from_loop_lag",
    "from_recovery",
    "from_retention",
    "from_stack_dumps",
    "from_sweep",
    "get_health",
    "judge_stages",
    "REPORT_BYTES",
    "Sweep",
    "read_latest",
    "read_report",
    "register",
    "runtime_tail",
]
