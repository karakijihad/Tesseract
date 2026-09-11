"""The graph, as something a canvas can draw.

``GET /api/autonomy/graph`` is the whole payload behind the graph surface. It
is a different question from ``/api/autonomy/atlas``: that one is the Atlas
room, four rows about whether the map is current, and it belongs on a
dashboard. This one is the map itself, and it exists because a canvas needs
every node it draws in one payload.

**Nothing here decides what the graph is.** Where a node sits is
`layout.py`'s, settled at build time so two visits draw the same picture.
How connected a node is, is `report.degrees`, the same count `report.hubs`
ranks by, so a big dot and a row in the hub list are one claim. What became
new since the last pass is the graph's own recorded `delta`, compared once by
the build and read here. A number this module worked out for itself would be
a second definition of a word `orchestrator/atlas/` already owns.

**It never sends the whole corpus, and it says how much it sent.** This
machine holds 3,514 nodes, 3,360 of them runs, and a picture that silently
shows a tenth of a library reads as a picture of all of it. `working_set`
below is the one rule, `surface.max_nodes_drawn` in `atlas.yaml` is the one
ceiling, and `drawn` carries both halves of the sentence the surface prints.

**Read once per change.** The same bargain `autonomy_atlas.py` makes: the
graph file is 2 MB and parsing it is not something to do on a poll, so the
reading is keyed on the file's own modification time and size.

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
import yaml

from tesseract.mirror.server.routes._isotime import iso as _iso
from tesseract.orchestrator.atlas import locate as atlas_locate
from tesseract.orchestrator.atlas import report as atlas_report
from tesseract.orchestrator.atlas import store as atlas_store
from tesseract.orchestrator.atlas.build import BUILDER_VERSION
from tesseract.orchestrator.atlas.config import load_atlas_config
from tesseract.orchestrator.atlas.model import (
    NodeKind,
    Provenance,
    Region,
    label_of,
    label_of_kind,
    label_of_link,
    label_of_provenance,
    region_of,
)
from tesseract.orchestrator.atlas.store import Atlas

log = logging.getLogger(__name__)


def working_set(atlas: Atlas, degree: dict[str, int], ceiling: int) -> list[str]:
    """Which records the picture draws, in one rule.

    Most connected first, then most recently added, then by id. Connections
    lead because they are what a map is for: a record nothing links to teaches
    nothing on a picture of links, and `orphans` in the Atlas room is where it
    is already reported. Recency breaks the tie because the body is thousands
    of runs at equal connectedness and the useful ones are the recent ones. The
    id breaks the last tie so the same graph draws the same picture, which is
    the whole of exit criterion 5.

    **The recency is `first_seen`, and that is measured rather than chosen.**
    `updated_at` is the moment the BUILD ran: 3,514 nodes on this machine and
    one distinct value between them, so ranking by it would have been a
    sentence the data cannot support. `first_seen` is when the graph first saw
    the record, which is what a person means by the newest thing on the map.

    Deliberately NOT per-compartment quotas. One ranking means the sentence
    beside the picture stays true for every compartment at once, where four
    quotas would need four sentences and would draw a compartment's newest
    runs while leaving a connected memory out.
    """
    ranked = sorted(
        atlas.nodes.values(),
        key=lambda node: (
            -degree.get(node.id, 0),
            -node.first_seen.timestamp(),
            node.id,
        ),
    )
    return [node.id for node in ranked[:ceiling]]


def _said(drawn: int, total: int) -> str:
    """What the surface prints over the picture. Written here because it is a
    claim about the payload rather than about the app, and the two numbers in
    it are the ones a reader has to have."""
    if total == 0:
        # A pass ran and found nothing. A real answer, and not the same claim
        # as no pass at all, which is the branch above this function.
        return (
            "the map was drawn and there was nothing to put in it, so there is "
            "nothing yet to see"
        )
    if drawn >= total:
        return f"every one of the {total:,} records this machine has mapped"
    return (
        f"{drawn:,} of {total:,} records: the most connected ones, and the most "
        "recently added where they are equally connected"
    )


def _nothing_to_draw(said: str) -> dict[str, Any]:
    """The shape of the payload when there is no picture, whatever the reason.
    The reason is the sentence: `built: null` tells the surface not to draw,
    and `said` is the only thing that separates a machine nobody has mapped
    from a file that cannot be read."""
    return {
        "built": None,
        "said": said,
        "regions": [],
        "kinds": [],
        "provenance": [],
        "links": [],
        "hubs": [],
        "orphans": [],
        "drawn": {"nodes": 0, "edges": 0, "edgesHidden": 0, "of": 0},
        "nodes": [],
        "edges": [],
        "delta": {"known": False, "nodes": [], "edges": []},
        "notReached": _not_reached(),
    }


def _payload(target: Path, ceiling: int) -> dict[str, Any]:
    if not target.exists():
        return _nothing_to_draw(
            "nothing has drawn the map on this machine yet, so how things "
            "connect is not known rather than empty"
        )
    atlas = atlas_store.load(target)
    if atlas.built_at is None:
        # `store.load` answers an unreadable file with an empty graph, which is
        # the right answer for a rebuild and the wrong one for a picture: every
        # build stamps when it ran, so a file that is here and carries no stamp
        # was not written by one. Drawing zero records over that would report a
        # corrupt file as an empty library.
        return _nothing_to_draw(
            "the map on disk could not be read, so what is in it is unknown "
            "rather than empty. The next nightly pass writes it again"
        )

    degree = atlas_report.degrees(atlas)
    keep = working_set(atlas, degree, ceiling)
    kept = set(keep)

    nodes = []
    for node_id in keep:
        node = atlas.nodes[node_id]
        x, y = atlas.positions.get(node_id, (0.0, 0.0))
        nodes.append(
            {
                "id": node.id,
                "kind": node.kind.value,
                "region": node.region.value,
                "title": node.title,
                "locator": node.locator,
                "x": x,
                "y": y,
                "degree": degree.get(node.id, 0),
                # When the graph first saw it. `updated_at` is the moment
                # the build ran and is the same for every node, so it is
                # not sent: a field that is one value 3,514 times reads as
                # a fact about the record and is a fact about the pass.
                "firstSeen": _iso(node.first_seen),
            }
        )

    edges = []
    hidden = 0
    for edge in atlas.edges.values():
        if edge.subject not in kept or edge.object not in kept:
            hidden += 1
            continue
        edges.append(
            {
                "id": edge.id,
                "subject": edge.subject,
                "object": edge.object,
                "type": edge.type,
                "provenance": edge.provenance.value,
                "creator": edge.creator.value,
                "locator": edge.locator,
                "confidence": edge.confidence,
                "assertedBy": edge.asserted_by,
                # Which side of the picture this link is about. An edge
                # between two compartments is a different claim from one
                # inside a compartment, and it is the claim that makes this
                # one brain rather than four piles.
                "crosses": (
                    atlas.nodes[edge.subject].region
                    != atlas.nodes[edge.object].region
                ),
            }
        )

    counts = atlas.counts_by_region()
    drawn_per_region: dict[Region, int] = {region: 0 for region in Region}
    drawn_per_kind: dict[NodeKind, int] = {kind: 0 for kind in NodeKind}
    for node_id in keep:
        drawn_per_region[atlas.nodes[node_id].region] += 1
        drawn_per_kind[atlas.nodes[node_id].kind] += 1

    # A position map that does not cover the graph is a build that added nodes
    # after the layout settled. The surface would draw them all on the origin,
    # so it is said out loud rather than drawn.
    unplaced = [node_id for node_id in keep if node_id not in atlas.positions]

    return {
        "built": {
            "at": _iso(atlas.built_at) if atlas.built_at else None,
            "builderVersion": atlas.builder_version,
            "stale": atlas.builder_version != BUILDER_VERSION,
            "nodes": len(atlas.nodes),
            "edges": len(atlas.edges),
            "conflicts": len(atlas.conflicts),
            "unplaced": len(unplaced),
        },
        "said": _said(len(keep), len(atlas.nodes)),
        "regions": [
            {
                "key": region.value,
                "label": label_of(region),
                "count": counts.get(region, 0),
                "drawn": drawn_per_region[region],
            }
            for region in Region
        ],
        # What each way of coming to know something MEANS. A table rather
        # than a word on every link: the same three strings would otherwise
        # ride 385 times.
        "provenance": [
            {"key": p.value, "label": label_of_provenance(p)} for p in Provenance
        ],
        # What each KIND OF LINK says, for the same reason and with the same
        # rule: only the ones on the picture, because a way of narrowing the
        # view that matches nothing is a control that reads as broken.
        "links": [
            {"key": link_type, "label": label_of_link(link_type)}
            for link_type in sorted({edge["type"] for edge in edges})
        ],
        # The most connected records, by `report.hubs` and by nothing else.
        # It is what `ATLAS.md` calls a hub, so the way into the picture and
        # the hub list in the page are one claim rather than two rankings that
        # agree today. Bounded to what is drawn: an id the canvas cannot find
        # is a way in that opens on nothing.
        "hubs": [
            node_id for node_id, _ in atlas_report.hubs(atlas) if node_id in kept
        ],
        # The other real way into the picture. An orphan IS a node, unlike a
        # dangling link's missing side, so the canvas can honestly draw one:
        # bounded to what is drawn, the same rule `hubs` follows and for the
        # same reason.
        "orphans": [
            node_id for node_id in atlas_report.orphans(atlas) if node_id in kept
        ],
        # The key to the colours, in the words the model already keeps. Only
        # the kinds actually on the picture: a legend naming a colour the
        # reader cannot find is a colour they will go looking for.
        "kinds": [
            {
                "key": kind.value,
                "label": label_of_kind(kind),
                "region": region_of(kind).value,
                "drawn": drawn,
            }
            for kind, drawn in drawn_per_kind.items()
            if drawn
        ],
        "drawn": {
            "nodes": len(nodes),
            "edges": len(edges),
            "edgesHidden": hidden,
            "of": len(atlas.nodes),
        },
        "nodes": nodes,
        "edges": edges,
        "delta": (
            {"known": False, "nodes": [], "edges": []}
            if atlas.delta is None
            else {
                "known": True,
                # Only what is on the picture. An id the canvas cannot find is
                # a filter that silently matches nothing.
                "nodes": [i for i in atlas.delta.nodes if i in kept],
                "edges": [i for i in atlas.delta.edges if i in atlas.edges],
            }
        ),
        "notReached": _not_reached(),
    }


def _not_reached() -> list[dict[str, str]]:
    """What this machine holds that the map does not cover, and why.

    `report.NOT_INDEXED` verbatim, the same declaration the Atlas room shows.
    It belongs on the picture too and for a stronger reason: a room saying a
    region is not built yet is a sentence, and a canvas drawing three
    compartments where four are declared is a picture that looks complete.
    """
    return [{"what": what, "why": why} for what, why in atlas_report.NOT_INDEXED]


#: The reading, keyed on the file it came from AND on how much was asked for. `atlas.json` is megabytes and
#: parsing it is the expensive half of this route, so a poll arriving between
#: two nightly passes pays for a `stat` and a small YAML read. The ceiling
#: is deliberately NOT cached with it: it is the operator's number, and a
#: room showing a changed setting as the old one for two minutes is a defect
#: this panel has already shipped once.
_cache: tuple[tuple[str, int, int], tuple[int, str], dict[str, Any]] | None = None
_lock = threading.Lock()


def _identity(path: Path) -> tuple[str, int, int]:
    """A missing file is its own identity, so the surface does not keep
    answering from the last graph it saw after one is deleted."""
    try:
        stat = path.stat()
    except OSError:
        return (str(path), -1, -1)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _ceiling() -> int:
    """How much the picture draws. Config is the authority; a config that
    cannot be read must not take the surface down, because the graph is still
    perfectly drawable at the shipped ceiling."""
    try:
        return load_atlas_config().surface.max_nodes_drawn
    except (OSError, ValueError, yaml.YAMLError) as exc:
        from tesseract.orchestrator.atlas.config import Surface

        log.warning("graph route: atlas.yaml could not be read (%s)", exc)
        return Surface().max_nodes_drawn


#: What the surface may ask for, beside the shipped ceiling. Asking for more
#: than this is not refused, it is clamped to the graph: the honest maximum is
#: "everything", and the payload already says how much of the whole it drew.
#:
#: **The reader chooses, not this file.** The config ceiling is what a picture
#: opens on, because a canvas full of four and a half thousand dots answers no
#: question; but a reader who wants the whole library has asked a real question
#: and refusing it would leave records reachable by nothing at all. Measured on
#: this machine: 4,640 records is 2.35 MB and 140 ms to build, and the canvas
#: repaints it in 26 ms against 4 ms at the shipped 600, so the picture is
#: still usable and dragging is no longer smooth. That is a trade the person
#: looking at it makes.
DRAW_EVERYTHING = "all"


def _asked_for(raw: str, ceiling: int, held: int) -> int:
    """How many records this request wants drawn.

    Anything unreadable falls back to the ceiling rather than raising: a
    surface that sent a bad number gets the picture it would have got anyway,
    and a query string is somebody else's input.
    """
    value = (raw or "").strip().lower()
    if not value:
        return ceiling
    if value == DRAW_EVERYTHING:
        return held
    try:
        asked = int(value)
    except ValueError:
        return ceiling
    return max(1, min(asked, held))


def graph(path: Path | None = None, drawn: str = "") -> dict[str, Any]:
    global _cache

    target = path or atlas_store.atlas_path()
    key = _identity(target)
    ceiling = _ceiling()
    cached = _cache
    if cached is not None and cached[0] == key and cached[1] == (ceiling, drawn):
        return cached[2]
    with _lock:
        # Checked again under the lock: two polls arriving together parse the
        # file once, which is the whole point of keeping it.
        cached = _cache
        if cached is not None and cached[0] == key and cached[1] == (ceiling, drawn):
            return cached[2]
        # The graph's own size bounds the ask, so `all` means all and a number
        # past the end means the same thing. Read from the file that is about
        # to be read anyway rather than guessed.
        held = len(atlas_store.load(target).nodes) if drawn else ceiling
        built = _payload(target, _asked_for(drawn, ceiling, held))
        _cache = (key, (ceiling, drawn), built)
        return built


async def get_graph(request: web.Request) -> web.Response:
    # How much of the map to draw, when the reader has said. Absent is the
    # shipped ceiling, which is what a picture opens on.
    drawn = (request.query.get("drawn") or "").strip()
    # A megabyte of JSON parsed and every edge walked, off the loop that
    # carries health, the socket and inbound turns.
    try:
        payload = await asyncio.to_thread(graph, None, drawn)
    except OSError as exc:
        log.warning("graph route: the map could not be read (%s)", exc)
        return web.json_response(
            {
                "error": (
                    "the map could not be read from disk, so nothing can be "
                    "drawn. The next nightly pass writes it again"
                )
            },
            status=503,
        )
    return web.json_response(
        {**payload, "observedAt": _iso(datetime.now(timezone.utc))}
    )


def record(node_id: str, path: Path | None = None) -> dict[str, Any] | None:
    """One record, or `None` when the graph does not hold that node.

    **The id is the whole of the input.** A caller never names a file: it names
    a node, the graph says whether that node exists, and
    `orchestrator/atlas/locate.py` is the only thing that turns a node into a
    path. What is readable through here is therefore exactly what the last
    nightly pass put on the map, and nothing else on this machine.

    The graph is re-read rather than kept: 56 ms for 3,514 nodes on this
    machine, off the loop, and a record is a click and not a poll. Holding the
    parsed graph resident for the life of the process to save that would be a
    second cache over one file, and the poll above is what the existing one is
    for.
    """
    atlas = atlas_store.load(path or atlas_store.atlas_path())
    node = atlas.nodes.get(node_id)
    if node is None:
        return None
    read = atlas_locate.read(node)
    return {
        "id": node.id,
        "kind": node.kind.value,
        "region": node.region.value,
        "title": node.title,
        "locator": read.locator,
        "properties": [
            {"key": key, "value": value} for key, value in read.properties
        ],
        "body": read.body,
        "bodyIsMarkdown": read.body_is_markdown,
        "truncated": read.truncated,
        # Empty when the record is here. When it is not, this is the whole
        # answer, and it says WHICH of the several ways of having nothing.
        "said": read.said,
        "aliases": list(read.aliases),
    }


async def get_record(request: web.Request) -> web.Response:
    """The record behind one node, by its id."""
    node_id = (request.query.get("id") or "").strip()
    if not node_id:
        return web.json_response({"error": "name a record"}, status=400)
    try:
        found = await asyncio.to_thread(record, node_id)
    except OSError as exc:
        log.warning("graph route: a record could not be read (%s)", exc)
        return web.json_response(
            {"error": "the record could not be read from disk"}, status=503
        )
    if found is None:
        # Not 404: the surface asked about something the MAP does not hold,
        # which is an answer about the map rather than a missing endpoint.
        return web.json_response(
            {
                "id": node_id,
                "said": (
                    "the map does not hold this any more. It may have been "
                    "drawn out by the last nightly pass"
                ),
            }
        )
    return web.json_response(found)


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/graph", get_graph)
    app.router.add_get("/api/autonomy/graph/record", get_record)


__all__ = [
    "DRAW_EVERYTHING",
    "get_graph",
    "get_record",
    "graph",
    "record",
    "register",
    "working_set",
]
