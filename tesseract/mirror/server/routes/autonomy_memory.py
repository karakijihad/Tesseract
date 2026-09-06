"""The library, and what last night did to it.

``GET /api/autonomy/memory`` is the Memory room. Three bands, and each one is
read from the thing that owns the fact rather than derived here:

* **the four trees** — what is on disk, counted and sized by walking them.
  A count is the only honest answer to "how big is the library" and nothing
  else keeps one.
* **last night** — the memory stages of the last nightly pass, as that run
  recorded them. The stage's own reason, never parsed: `outcome_reason` is
  documented as free plain language and reading counts out of it would be a
  second definition of what a stage did, written where the stage cannot see
  it.
* **retrieval** — whether a question asked now can be answered by vectors or
  only by keywords. The live bundle's own indexes, because a rebuild that
  happened at 23:14 says nothing about whether Ollama is up at noon.

**A tree that does not exist and an empty tree are different claims**, and the
room draws them differently: the liveness contract's rule, in the one room
where a missing tree is most likely to read as a quiet one.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes._bands import band_for

from tesseract import paths
from tesseract.mirror.server.routes._isotime import iso as _iso
from tesseract.mirror.server.routes.autonomy_entry import stage_summaries, stages_of
from tesseract.orchestrator.liveness import OperationalState, label_of
from tesseract.orchestrator.obligation import as_payload as wants
from tesseract.scheduler.log import iter_runs, runs_path

log = logging.getLogger(__name__)

# One unwrap for this room's producers, named so a warning says which surface
# went thin. Four rooms had four copies of it.
_band = band_for("memory")

# The row the library is settled by. Its memory stages are what "last night"
# means, and naming it here rather than scanning every row keeps the band
# about the nightly pass rather than about whatever ran most recently.
NIGHTLY = "consolidate"

# The stages of that row this room speaks for. Named rather than pattern
# matched on the word "memory": `atlas_build` and `dream_cycle` are about the
# library and say so nowhere in their names, and a substring rule would take
# `memory_bundle` reads from an unrelated row the day one appears.
LIBRARY_STAGES = (
    "librarian_heartbeat",
    "dream_cycle",
    "memory_lint",
    "memory_scrub",
    "memory_relink",
    "index_rebuild",
    "atlas_build",
)

# How many entries a tree walk will look at before it stops and says so. A
# library is thousands of small files and this room is polled; the cap bounds
# the walk and the count says plainly that it is a floor rather than a total,
# because a number silently cut is worse than one that admits it.
#
# It counts ENTRIES and not files, which is the half that makes it a bound: a
# directory costs nothing against a file cap, so a tree of directories is
# unbounded under one. Together with not descending a symlink below, that is
# what stops a link pointing back up its own tree from walking forever.
WALK_CAP = 20_000


def _tree(name: str, root: Path, what: str) -> dict[str, Any]:
    """One tree: how many files, how much space, and when it last changed."""
    if not root.exists():
        return {
            "name": name,
            "state": OperationalState.NOT_INSTRUMENTED.value,
            "label": label_of(OperationalState.NOT_INSTRUMENTED),
            **wants(OperationalState.NOT_INSTRUMENTED),
            "said": f"nothing has written {what} on this machine yet",
            "at": None,
            "value": "",
        }
    files = 0
    total = 0
    newest: float = 0.0
    seen = 0
    capped = False
    stack = [root]
    while stack and not capped:
        current = stack.pop()
        # Walked lazily, and the OSError guard is around the ITERATION rather
        # than the call: `iterdir` is a generator, so the directory is read on
        # the first step and an unreadable one raises there. Listing it whole
        # first would also mean one directory with a million entries in it is
        # read whole however low the cap is set.
        try:
            for entry in current.iterdir():
                seen += 1
                if seen > WALK_CAP:
                    capped = True
                    break
                try:
                    # A real directory is walked; a link that points at one is
                    # counted where it stands. Following it is how a tree that
                    # refers back to itself becomes a walk with no end.
                    if not entry.is_symlink() and entry.is_dir():
                        stack.append(entry)
                        continue
                    stat = entry.stat()
                except OSError:
                    continue
                files += 1
                total += stat.st_size
                newest = max(newest, stat.st_mtime)
        except OSError:
            continue
    counted = f"at least {files:,} files" if capped else f"{files:,} files"
    # The size stopped adding up at the same entry the count did, so it is the
    # same floor and says so in the same words.
    size = f"at least {_size(total)}" if capped else _size(total)
    return {
        "name": name,
        # A tree on disk is not a health reading. It is there, and how much of
        # it there is, is the fact.
        "state": OperationalState.IDLE.value,
        "label": label_of(OperationalState.IDLE),
        **wants(OperationalState.IDLE),
        "said": f"{counted} of {what}",
        "at": _iso(datetime.fromtimestamp(newest, tz=timezone.utc)) if newest else None,
        "value": size,
    }


def _size(total: int) -> str:
    """Bytes in the unit a person reads. Never a raw byte count."""
    if total < 1024:
        return f"{total} B"
    if total < 1024 * 1024:
        return f"{total / 1024:.0f} KB"
    if total < 1024 * 1024 * 1024:
        return f"{total / (1024 * 1024):.0f} MB"
    return f"{total / (1024 * 1024 * 1024):.1f} GB"


#: How long a reading of the trees stands before it is walked again. One
#: panel refresh asks for these twice, from two routes that do not know about
#: each other: the Memory room reads them, and the writer's counted facts read
#: them again. Walking four trees twice per poll is the same answer paid for
#: twice. Well under how often a person looks, and well over how long one
#: refresh takes, so the two callers in a refresh share one walk and a memory
#: written now shows up on the next.
TREES_TTL_SECONDS = 30.0

_trees_cache: tuple[tuple[str, ...], float, list[dict[str, Any]]] | None = None
_trees_lock = threading.Lock()


def _tree_roots() -> list[tuple[str, Path, str]]:
    """The four things the library is made of, and where each is written.

    Each path is the one its own writer uses: `skill_refinement.py` writes
    under the workspace and `user_agents_dir` is where a card the assistant
    built lands, so a tree named here is the tree that is actually written to
    rather than a guess that would report zero forever.
    """
    home = paths.home_dir()
    return [
        ("memory-store", home / "memory-store", "what it remembers"),
        ("vault", home / "vault", "what it has read"),
        (
            "skills",
            paths.workspace_dir() / "skills",
            "ways of working it has written down",
        ),
        ("agents", paths.user_agents_dir(), "cards it can delegate to"),
    ]


def trees() -> list[dict[str, Any]]:
    """The library on disk, walked at most once per `TREES_TTL_SECONDS`.

    Keyed on the roots themselves, so a different home is a different reading
    rather than the previous one handed back. It holds ONE entry: there is one
    home in a running app, and the key is what keeps a second one from being
    answered wrongly.
    """
    global _trees_cache

    roots = _tree_roots()
    key = tuple(str(path) for _, path, _ in roots)
    cached = _trees_cache
    if cached is not None and cached[0] == key:
        if time.monotonic() - cached[1] < TREES_TTL_SECONDS:
            return cached[2]
    with _trees_lock:
        # Checked again under the lock: two threads arriving together must
        # walk once, not twice, which is the whole point of the cache.
        cached = _trees_cache
        if cached is not None and cached[0] == key:
            if time.monotonic() - cached[1] < TREES_TTL_SECONDS:
                return cached[2]
        walked = [_tree(name, path, what) for name, path, what in roots]
        _trees_cache = (key, time.monotonic(), walked)
        return walked


def last_night() -> tuple[list[dict[str, Any]], str]:
    """What the last nightly pass did to the library, as it recorded it.

    The stage rows of the newest `consolidate` run, filtered to the stages
    that touch the library. Their reasons are their own words, rendered as
    they were written: a route that read `promoted=12` out of one would be
    deciding what a stage did somewhere the stage cannot see.

    Takes no moment on purpose: the band is the newest `consolidate` run there
    is, whenever it ran, and a parameter the body ignores tells a reader it is
    anchored to something it is not.

    Returns the steps AND a sentence for when there are none, because there are
    three ways to have none and they are not the same claim: nothing has ever
    run, the run log could not be read, or a pass ran and touched nothing in
    the library. Collapsing them means an I/O fault hides behind a reassuring
    message, which is the fault the two bands either side of this one already
    refuse to make.
    """
    try:
        rows = [r for r in iter_runs(runs_path()) if r.get("job_name") == NIGHTLY]
    except OSError:
        log.warning("memory route: the run log could not be read")
        return [], (
            "the record of what runs on this machine could not be read, so "
            "what last night did to the library is unknown rather than nothing"
        )
    if not rows:
        return [], "no nightly pass has touched it on this machine yet"
    summaries = stage_summaries(NIGHTLY)
    steps = stages_of(rows[-1].get("payload"), summaries)
    kept = [s for s in steps if s["stage"] in LIBRARY_STAGES]
    if kept:
        return kept, ""
    return [], (
        "the last nightly pass ran and none of its library stages did, so "
        "nothing was added, pruned or re-indexed"
    )


def retrieval(app: Any) -> list[dict[str, Any]]:
    """Whether a question asked NOW can be answered, and by what.

    Off the live bundle rather than off the last rebuild: a pass that indexed
    everything at 23:14 says nothing about whether the embedding model is
    reachable at noon, and a room that reported the pass would show a green
    line over a retrieval that has silently fallen back to keywords.
    """
    bundle = app.get("memory_bundle") if hasattr(app, "get") else None
    if bundle is None:
        return [
            {
                "name": "retrieval",
                "state": OperationalState.NOT_INSTRUMENTED.value,
                "label": label_of(OperationalState.NOT_INSTRUMENTED),
                **wants(OperationalState.NOT_INSTRUMENTED),
                "said": "nothing in this process holds the indexes, so it cannot be asked",
                "at": None,
                "value": "",
            }
        ]

    out: list[dict[str, Any]] = []
    embeddings = getattr(bundle, "embeddings", None)
    if embeddings is None:
        # Nothing holds one only when the config points somewhere other than
        # Ollama, or switches embeddings off. That is a setting, not a fault,
        # and drawing it as a fault is how a panel teaches its reader to stop
        # believing the colour.
        out.append(
            {
                "name": "vector search",
                "state": OperationalState.NOT_INSTRUMENTED.value,
                "label": label_of(OperationalState.NOT_INSTRUMENTED),
                # Somebody chose this in the model settings. The state is
                # right, and asking about it every time the panel is opened
                # would be asking about a decision already made.
                **wants(OperationalState.NOT_INSTRUMENTED, by_choice=True),
                "said": (
                    "switched off in the model settings, so every question is "
                    "answered by keywords alone and a memory worded "
                    "differently will not be found"
                ),
                "at": None,
                "value": "",
            }
        )
    else:
        try:
            held = len(embeddings.snapshot_ids())
        except Exception:  # noqa: BLE001 — a room says less rather than 500ing
            log.exception("memory route: the vector index could not be counted")
            held = 0
        # What the last real call found, never a probe of our own: this route
        # is polled, and a panel that probes hardware on every tick is a
        # second load on the thing it is reporting. `answered` is None until
        # something has actually been embedded, which is not the same as
        # working and is not drawn as though it were.
        answered: bool | None = None
        answered_at = None
        reach = getattr(embeddings, "reachability", None)
        if callable(reach):
            try:
                answered, answered_at = reach()
            except Exception:  # noqa: BLE001 — a room says less rather than 500ing
                log.exception("memory route: the vector index health could not be read")
        if answered is False:
            out.append(
                {
                    "name": "vector search",
                    "state": OperationalState.DEGRADED.value,
                    "label": label_of(OperationalState.DEGRADED),
                    **wants(OperationalState.DEGRADED),
                    # No cause named on purpose. A timeout here can mean a busy
                    # model rather than a stopped one, and this line said
                    # "Ollama may be down" 190 times in one evening against an
                    # Ollama that answered a hand probe in under a second.
                    "said": (
                        "the last request to the embedding model did not "
                        "finish, so questions are answered by keywords alone "
                        "until it answers again. It recovers on its own"
                    ),
                    "at": _iso(answered_at),
                    "value": f"{held:,} indexed",
                }
            )
        else:
            out.append(
                {
                    "name": "vector search",
                    "state": OperationalState.IDLE.value,
                    "label": label_of(OperationalState.IDLE),
                    **wants(OperationalState.IDLE),
                    "said": (
                        "answering, so a memory worded differently is still found"
                        if answered
                        else "ready, so a memory worded differently is still found"
                    ),
                    "at": _iso(answered_at),
                    "value": f"{held:,} indexed",
                }
            )

    fts = getattr(bundle, "fts_index", None)
    if fts is None:
        out.append(
            {
                "name": "keyword search",
                "state": OperationalState.NOT_INSTRUMENTED.value,
                "label": label_of(OperationalState.NOT_INSTRUMENTED),
                **wants(OperationalState.NOT_INSTRUMENTED),
                "said": "nothing in this process holds it",
                "at": None,
                "value": "",
            }
        )
    else:
        try:
            rows = len(fts.all_ids())
        except Exception:  # noqa: BLE001 — a room says less rather than 500ing
            log.exception("memory route: the keyword index could not be counted")
            rows = 0
        out.append(
            {
                "name": "keyword search",
                "state": OperationalState.IDLE.value,
                "label": label_of(OperationalState.IDLE),
                **wants(OperationalState.IDLE),
                "said": "answering",
                "at": None,
                "value": f"{rows:,} indexed",
            }
        )
    return out


async def get_memory(request: web.Request) -> web.Response:
    """The three bands, each from the producer that owns it."""
    now = datetime.now(timezone.utc)
    # File walks and a SQLite count on the loop that carries health, the
    # socket and inbound turns.
    grown, night, reach = await asyncio.gather(
        asyncio.to_thread(trees),
        asyncio.to_thread(last_night),
        asyncio.to_thread(retrieval, request.app),
        return_exceptions=True,
    )

    steps, said = _band(
        night,
        ([], "the record could not be read, so what last night did is unknown"),
    )
    return web.json_response(
        {
            "trees": _band(grown, []),
            "lastNight": steps,
            # Why there is nothing, when there is nothing. The backend answers,
            # so no view restates what an empty band means.
            "lastNightSaid": said,
            "retrieval": _band(reach, []),
            "observedAt": _iso(now),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/memory", get_memory)


__all__ = [
    "LIBRARY_STAGES",
    "NIGHTLY",
    "WALK_CAP",
    "get_memory",
    "last_night",
    "register",
    "retrieval",
    "trees",
]
