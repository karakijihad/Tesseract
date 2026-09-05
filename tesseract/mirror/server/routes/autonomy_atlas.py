"""The map of how everything connects, and what the last drawing of it found.

``GET /api/autonomy/atlas`` is the Atlas room. It is deliberately small: the
graph itself is its own surface, because a canvas demands the whole screen and
the one thing it cannot help with is a failed run. What belongs here is whether
the map is current, what it could not make sense of, and the way in.

Three bands, each read from the thing that owns the fact:

* **the graph as it stands** — the file on disk, and the four questions
  `report.py` already answers about one: how much is in it, what disagrees,
  what points at a record that is not there, and what nothing points at. The
  report module decides what each of those means; this route does not
  re-derive them, because two definitions of an orphan is one too many.
* **what it does not reach** — `report.NOT_INDEXED`, verbatim. A map that
  quietly stops at the edge of what it indexed reads as a map of everything,
  and the operator would take an empty region for an idle one.
* **the last pass over it** — the atlas stages of the newest nightly run, as
  that run recorded them. `atlas_build` also appears in the Memory room's own
  band, and that is not a duplication: there it is one step of what last night
  did to the library, here it is the record of when this map was last drawn.

**Both files are read once per change, not once per poll.** The atlas is
derived nightly and this room is polled, so each reading is keyed on the
identity of the file it came from: same file, same answer, and a rebuild is
picked up on the next poll. That holds for the run log as well as for the
graph, and it did not always: `runs.jsonl` is the largest log this machine
keeps, and answering one question about the last pass reparsed all of it
every ninety seconds while the room was open.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes._bands import band_for
from tesseract.mirror.server.routes._isotime import iso as _iso
from tesseract.mirror.server.routes.autonomy_entry import stage_summaries, stages_of
from tesseract.orchestrator.atlas import report as atlas_report
from tesseract.orchestrator.atlas import store as atlas_store
from tesseract.orchestrator.atlas.build import BUILDER_VERSION
from tesseract.orchestrator.liveness import OperationalState, label_of
from tesseract.scheduler.log import iter_runs, runs_path

log = logging.getLogger(__name__)

# One unwrap for this room's producers, named so a warning says which surface
# went thin.
_band = band_for("atlas")

# The row that draws the map. Its atlas stages are what "the last pass" means,
# and naming the row rather than scanning every run keeps the band about the
# nightly drawing rather than about whatever ran most recently.
NIGHTLY = "consolidate"

# The stages of that row this room speaks for: the one that draws the map and
# the one that checks the drawing still matches what it was drawn from.
ATLAS_STAGES = ("atlas_build", "atlas_verify")


def _row(
    name: str,
    state: OperationalState,
    said: str,
    *,
    at: datetime | None = None,
    value: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "state": state.value,
        "label": label_of(state),
        "said": said,
        "at": _iso(at) if at else None,
        "value": value,
    }


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


#: The reading, keyed on the file it was taken from. A poll that arrives
#: between two nightly passes gets the same answer for the price of a `stat`,
#: and the poll after a rebuild reads the new file: identity is the file's own
#: size and modification time, so nothing has to be invalidated by hand.
_graph_cache: tuple[tuple[str, int, int], list[dict[str, Any]]] | None = None
_graph_lock = threading.Lock()


def _identity(path: Path) -> tuple[str, int, int]:
    """What makes this the same reading. A missing file is its own identity,
    so the room does not keep answering from the last one it saw."""
    try:
        stat = path.stat()
    except OSError:
        return (str(path), -1, -1)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def graph(path: Path | None = None) -> list[dict[str, Any]]:
    """The map as it stands, in four rows a person can act on.

    Every count comes from `report.py`, which is where what an orphan is and
    what a link into nothing is are decided. The states are readings and not
    faults except where something is genuinely owed: a record nothing points at
    is still found by searching for its words, so it is a fact about the shape
    of the library rather than a fault in it.
    """
    global _graph_cache

    target = path or atlas_store.atlas_path()
    key = _identity(target)
    cached = _graph_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    with _graph_lock:
        # Checked again under the lock: two polls arriving together read the
        # file once, which is the whole point.
        cached = _graph_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        rows = _read(target)
        _graph_cache = (key, rows)
        return rows


def _read(target: Path) -> list[dict[str, Any]]:
    if not target.exists():
        return [
            _row(
                "the map",
                OperationalState.NOT_INSTRUMENTED,
                "nothing has drawn it on this machine yet, so how things "
                "connect is not known rather than empty",
            )
        ]
    atlas = atlas_store.load(target)
    if not atlas.nodes:
        # A file with nothing in it is a real answer and not a missing one:
        # the pass ran and found nothing to draw.
        return [
            _row(
                "the map",
                OperationalState.IDLE,
                "it was drawn and there was nothing to put in it, so there is "
                "nothing yet to remember or to have read",
                at=atlas.built_at,
            )
        ]

    stale = atlas.builder_version != BUILDER_VERSION
    rows = [
        _row(
            "the map",
            OperationalState.DEGRADED if stale else OperationalState.IDLE,
            (
                "drawn by an older version of the drawing, so what it shows is "
                "behind what the app would draw now. The next nightly pass "
                "redraws it"
                if stale
                else "drawn from what it remembers and what it has read"
            ),
            at=atlas.built_at,
            value=(
                f"{_plural(len(atlas.nodes), 'thing', 'things')}, "
                f"{_plural(len(atlas.edges), 'connection', 'connections')}"
            ),
        )
    ]

    disagree = len(atlas.conflicts)
    rows.append(
        _row(
            "records that disagree",
            OperationalState.DEGRADED if disagree else OperationalState.IDLE,
            (
                _plural(disagree, "pair of records says", "pairs of records say")
                + " something different, and both sides stand until you pick one"
                if disagree
                else "nothing in it contradicts anything else in it"
            ),
            value=str(disagree) if disagree else "",
        )
    )

    loose = atlas_report.dangling(atlas)
    rows.append(
        _row(
            "connections into nothing",
            OperationalState.DEGRADED if loose else OperationalState.IDLE,
            (
                _plural(len(loose), "connection names", "connections name")
                + " a record that is not here, usually one that was deleted "
                "after something else pointed at it"
                if loose
                else "every connection names a record that is here"
            ),
            value=str(len(loose)) if loose else "",
        )
    )

    alone = atlas_report.orphans(atlas)
    rows.append(
        _row(
            "nothing points at these",
            # A fact about the shape of the library, not a fault: searching
            # still finds them by their words. What they cannot do is turn up
            # because something else was relevant.
            OperationalState.IDLE,
            (
                _plural(len(alone), "thing has", "things have")
                + " nothing pointing at them, so following connections never "
                "arrives. Searching for their words still finds them"
                if alone
                else "everything in it can be reached by following connections"
            ),
            value=str(len(alone)) if alone else "",
        )
    )
    return rows


def not_reached() -> list[dict[str, Any]]:
    """What this machine holds that the map does not cover, and why.

    `report.NOT_INDEXED` is the declaration, kept beside the builders it is
    about. Restating it here would be a second list to keep in step, and it is
    the list that says why "everything connected" is the goal rather than the
    state.
    """
    return [
        _row(what, OperationalState.NOT_INSTRUMENTED, why)
        for what, why in atlas_report.NOT_INDEXED
    ]


#: The last pass, keyed on the run log it was read from — the same bargain
#: `_graph_cache` makes, and it was missing here while this module's own
#: docstring claimed both bands kept it. `runs.jsonl` is the largest log this
#: machine keeps and it is read WHOLE to answer one question, so an open room
#: was paying for that every ninety seconds, at a cost that grows with uptime.
_pass_cache: tuple[tuple[str, int, int], tuple[list[dict[str, Any]], str]] | None = None
_pass_lock = threading.Lock()


def last_pass() -> tuple[list[dict[str, Any]], str]:
    """What the last nightly pass did to the map, as it recorded it.

    The same three ways of having nothing the Memory room distinguishes, for
    the same reason: nothing has ever run, the record could not be read, or a
    pass ran and did not touch the map. One sentence for all three would let an
    unreadable record hide behind a reassuring message.
    """
    global _pass_cache

    key = _identity(runs_path())
    cached = _pass_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    with _pass_lock:
        cached = _pass_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        read = _read_last_pass()
        _pass_cache = (key, read)
        return read


def _read_last_pass() -> tuple[list[dict[str, Any]], str]:
    try:
        rows = [r for r in iter_runs(runs_path()) if r.get("job_name") == NIGHTLY]
    except OSError:
        log.warning("atlas route: the run log could not be read")
        return [], (
            "the record of what runs on this machine could not be read, so "
            "when the map was last drawn is unknown rather than never"
        )
    if not rows:
        return [], "no nightly pass has drawn it on this machine yet"
    steps = stages_of(rows[-1].get("payload"), stage_summaries(NIGHTLY))
    kept = [s for s in steps if s["stage"] in ATLAS_STAGES]
    if kept:
        return kept, ""
    return [], (
        "the last nightly pass ran and the steps that draw the map did not, "
        "so it still shows what the pass before it found"
    )


async def get_atlas(request: web.Request) -> web.Response:
    """The three bands, each from the producer that owns it."""
    del request
    now = datetime.now(timezone.utc)
    # A file read and a walk of every edge, off the loop that carries health,
    # the socket and inbound turns.
    rows, pass_over = await asyncio.gather(
        asyncio.to_thread(graph),
        asyncio.to_thread(last_pass),
        return_exceptions=True,
    )
    steps, said = _band(
        pass_over,
        ([], "the record could not be read, so when it was last drawn is unknown"),
    )
    return web.json_response(
        {
            "graph": _band(rows, []),
            "notReached": not_reached(),
            "lastPass": steps,
            # Why there is nothing, when there is nothing. The backend answers,
            # so no view restates what an empty band means.
            "lastPassSaid": said,
            "observedAt": _iso(now),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/atlas", get_atlas)


__all__ = [
    "ATLAS_STAGES",
    "NIGHTLY",
    "get_atlas",
    "graph",
    "last_pass",
    "not_reached",
    "register",
]
