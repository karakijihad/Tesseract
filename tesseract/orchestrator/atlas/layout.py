"""Where each node sits, decided once at build time.

AR-9 renders this; it does not settle it. A view that computes its own layout
computes a different one every time it mounts, and a map whose shape changes
when you open it is a map nobody can learn. So the coordinates are part of the
derived graph, written beside the nodes they belong to.

**Position says what is connected.** That is the whole reason to draw a graph
rather than list it, and it is what the first version of this file did not do:
it ordered a compartment's nodes by degree and stepped round a wedge by the
golden angle, so where a record sat had nothing to do with what it touched. On
the live library that measured as a linked pair sitting 37.5 units apart
against 68.9 for a random pair, and all of that gap came from both ends
usually being in the same wedge. A reader could not answer "what clusters" or
"why is this connected" from the picture, which is the whole of what a map is
for.

**Every property this has to hold, held at once.** Editing this file against
one of them is how the previous version lost another, so they are written down
together and a change is measured against the list rather than against
whatever prompted it:

1. **The same graph gives the same picture.** No clock, no random number
   generator, no dependence on the order a dict happens to be in. Nodes are
   walked in sorted id order and a new node's starting place comes from a hash
   of its id, not from `random`.
2. **Rounded**, so a float differing in its last bit cannot read as the map
   having moved.
3. **Every node gets a place and nothing else does.** An empty graph has an
   empty map.
4. **Adding one record does not rearrange the map.** A force layout is chaotic
   from a cold start, so this pass is warm started from the previous build's
   positions: a record that was already there begins where it ended and stays
   near it. This is the property the first version bought by refusing to use
   forces at all, and warm starting is what buys it back.
5. **The compartments read as four.** Each owns a DIRECTION, and every node is
   pulled gently along it. Gently on purpose: a link that crosses between two
   compartments is the thing that makes this one brain rather than four piles,
   and a hard boundary would hide exactly that by refusing to let the two ends
   near each other.
6. **The middle of the picture is the middle of the library.** How far out a
   record sits is what its connectedness earns, so the main records are in the
   centre and the leaves are around them, which is the operator's ask and the
   thing a graph is supposed to do. The direction is the compartment's and the
   radius is `pull_for`'s, and they are separate forces because they are
   separate claims: `ANCHOR_PULL` is weak so a crossing link can drag a record
   round, and `RADIAL_PULL` is firmer because at the anchor's strength the
   forces outvoted it every time and a hub settled further out than its own
   leaves.
7. **Records nothing links to do not pretend otherwise.** Most of the live
   library has no link at all. Settling them with forces would be an expensive
   way to produce noise, so they fill the rim of their own compartment's
   wedge, outside everything that is connected, which both says what they are
   and keeps them out of the way.
8. **It fits in a nightly build.** Forces run over the linked records only,
   which is 472 of 4,640 on the live library, so the pairwise work is small
   enough to do exactly rather than approximate. `NOT_PLACING` is what keeps
   that true as the graph gains links: a kind of link that joins thousands of
   records to one is a fact about them and not a statement about where they
   sit, and counting it would put the whole body in an exact pairwise solver.

   **The cost, measured 2026-09-02 and not estimated.** 472 records is 35
   seconds of solver, in a `run_build` of 63. It scales as the square: 200
   records is 6 seconds, 300 is 15, 448 is 36. An earlier note in this repo
   said 7.8 seconds and that number was simply wrong; four repeat measurements
   say otherwise. Warm or cold makes no difference, because the iteration count
   is fixed either way. `float32` for the pairwise block was measured at 1.25
   times faster and rejected: it is not enough to matter and it puts property 1
   at the mercy of what a platform's floats round to.

**What a rescale is and is not.** The settled picture is scaled once at the end
so the whole map fills a fixed disc. That moves every coordinate when the
extent changes, and it is deliberately not a violation of 4: it preserves every
distance and every angle in the picture, so the arrangement a person learnt is
the arrangement they come back to. What 4 forbids is rearrangement.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np

from tesseract.orchestrator.atlas import report
from tesseract.orchestrator.atlas.model import Region
from tesseract.orchestrator.atlas.store import Atlas

# The disc every node lands in, in abstract units. A renderer scales these; it
# does not reinterpret them.
OUTER_RADIUS = 100.0

# Enough decimals that two nodes never collide by rounding, few enough that a
# float that differs in its last bit cannot make two builds disagree.
PRECISION = 4

# The golden angle, as a fraction of a turn. Successive nodes in a band step
# round it by this much, which is what stops them lining up in spokes: no whole
# number of steps ever returns to the same angle.
_GOLDEN_STEP = 2.0 - (1.0 + math.sqrt(5.0)) / 2.0

# Fixed order, so a compartment's direction is the same in every build and on
# every machine. Adding a region moves the ones after it, which is a picture
# change and belongs with the build version that introduces it.
_ORDER: tuple[Region, ...] = tuple(Region)

# How far a compartment's middle sits from the middle of the map, before the
# final scaling. Everything below is in these units.
ANCHOR_RADIUS = 60.0

# How far apart two linked records want to sit. The one length the rest of the
# force constants are expressed against.
REST = 12.0
# How hard a compartment holds its own. Small, because a link that crosses
# between two of them has to be able to win.
ANCHOR_PULL = 0.010
# How tightly a hub's own leaves pack around it. The disc holding them has a
# radius of `LEAF_SPREAD * sqrt(count)`, so a hub with four leaves gets a small
# ring and one with 1,674 gets the big fan the operator's own reference picture
# shows. Area per leaf rather than a fixed ring, because a ring of 1,674 would
# be a circle nothing fits inside.
LEAF_SPREAD = REST * 0.62
# How many passes the forces get, and how far a node may move on the first one.
# Both fixed: an iteration count that depended on convergence would make the
# picture depend on how close the last build happened to land.
ITERATIONS = 500
STEP_FROM = REST * 1.5

# How much of that first step a record that was ALREADY PLACED may take.
#
# Property 4 is what this is for, and it was broken until it was measured. The
# cooling schedule started at `STEP_FROM` on every build, warm or cold, so
# every pass re-heated a map that was already settled: measured over six
# consecutive passes of an unchanged graph, the furthest record moved between
# 12 and 29 units on a 100-unit map, every time, without ever converging. The
# mean was under half a unit, which is why it had never been noticed — most of
# the map held still while a handful of records swung across it each night.
#
# So a record the last build placed cools from a tenth of the step, and a
# record nobody has placed still gets the whole of it. That is the property
# said in one line: what was already there begins where it ended and stays
# near it, and what is new is free to find where it belongs. The same
# measurement after: at most 7.1 units over four repeat passes, against 28.8
# before, and the median pair of records changed its separation by 0.3 percent.
#
# A tenth rather than a fiftieth, also measured: 0.05 and 0.02 came back at 8.8
# and 6.6, so the residual is not the per-step cap at all. It is the forces
# still relaxing, which is a system converging rather than a map wandering, and
# tightening the cap further only costs a warm record the room to adjust.
#
# NOT an iteration count that depends on convergence, which this file rejects
# for a different and still-good reason. Whether a record has a warm position
# is a fact about the inputs, so both branches are fixed and the same graph
# still gives the same picture.
WARM_STEP_SHARE = 0.1

# How far a record the last build placed may end up from where it was, over a
# pass in which the graph did not change. A tenth of the map's radius, which
# `test_the_map_sits_still` measures rather than assumes. It is a promise about
# the picture and not a solver tolerance: past this the map a person learnt is
# not the map they came back to.
SETTLED_WITHIN = OUTER_RADIUS * 0.1
# Nodes further apart than this ignore each other. Without it a compartment
# pushes on the far side of the map and the four collapse toward one ring.
REPULSION_RANGE = REST * 4.0

# How much room a compartment's unlinked records fill around it. Absolute, and
# the same for every compartment, for two measured reasons. Sized by count, the
# body's three thousand six hundred runs became a donut that was the whole
# picture and squashed everything connected into a speck. Sized by how far the
# linked ones reach, one record thrown wide by the forces took the band with
# it. A fixed halo also means adding records to a compartment does not move
# the other three.
#
# The ceiling is what keeps the four apart: their middles sit `ANCHOR_RADIUS`
# from the origin and a quarter turn apart, so two adjacent ones are
# `ANCHOR_RADIUS * sqrt(2)` from each other and two halos of this size leave a
# visible gap between them.
LOOSE_GAP = REST * 2.0
LOOSE_MAX = ANCHOR_RADIUS * 0.6

# How much of its compartment's wedge the rim of unlinked records fills. Under
# one, so two neighbouring rims leave a gap where they meet and the four are
# still four at the outside as well as at the middle.
LOOSE_WEDGE = 0.86


def sector(region: Region) -> tuple[float, float]:
    """The angular slice a compartment owns, in radians, start and width.

    Its middle is where the compartment is pulled toward. The slice itself is
    no longer a fence: a node may sit outside it when something in another
    compartment holds on to it, which is the case worth seeing.
    """
    width = 2.0 * math.pi / len(_ORDER)
    return _ORDER.index(region) * width, width


def anchor(region: Region, pull: float = 1.0) -> tuple[float, float]:
    """Where a record is pulled toward: its compartment's direction, and how
    far out along it.

    **The compartment decides the angle and how connected the record is
    decides the radius.** That is the operator's ask, in one line: the main
    things in the middle, the leaves around the outside. It was one anchor per
    compartment, at a fixed radius, so a hub and a record nothing points at
    were pulled to the same spot and the picture had four dense knots and no
    centre.

    The angle is kept, so property 5 still holds: each compartment owns a
    direction and its records fan out along it. What changed is that the
    fanning is now BY centrality rather than by whatever the forces did, so
    the middle of the picture is the middle of the library.

    An earlier version put every compartment's hubs at the origin and was
    rejected for landing them on top of each other. This does not: `pull`
    never reaches zero, so the four still separate at the centre, and the
    repulsion between them is what opens the rest.
    """
    start, width = sector(region)
    middle = start + width / 2.0
    radius = ANCHOR_RADIUS * pull
    return radius * math.cos(middle), radius * math.sin(middle)


#: How far in the most connected record is pulled, as a share of the full
#: compartment radius, and how far out a record with one link sits.
#:
#: The floor is not zero. At zero every compartment's hubs are pulled to one
#: point and the four stop being four, which is the version this file rejected
#: once already; at `CENTRE_PULL` they are near the middle and still a quarter
#: turn apart, so the centre reads as one cluster of four colours rather than
#: as a single ball.
CENTRE_PULL = 0.22
EDGE_PULL = 1.0

#: How many connections make a record as central as the picture goes. Past
#: this the curve is flat, so the difference between a hub with 64 links and
#: one with 1,683 is not drawn as distance: both are the middle of the
#: library, and spending the whole radius separating them would push
#: everything else to the rim.
MIDDLE_AT = 64


def _seeded(node_id: str) -> tuple[float, float]:
    """A starting offset for a node nobody has placed before.

    From a hash of the id rather than a random number generator, so it is the
    same on every run and on every machine. Python's own `hash` is salted per
    process and would make two builds of one graph disagree.
    """
    digest = hashlib.blake2b(node_id.encode("utf-8"), digest_size=8).digest()
    angle = int.from_bytes(digest[:4], "big") / 2**32 * 2.0 * math.pi
    radius = REST * (0.5 + int.from_bytes(digest[4:], "big") / 2**32)
    return radius * math.cos(angle), radius * math.sin(angle)


#: Links that say what a record IS rather than where it belongs.
#:
#: `ran` joins a run to the part of the machine it is a run of. There are 2,357
#: of them on the live graph and 1,674 point at one node, so pulling every one
#: of them into the force solver would settle 1,674 identical records onto a
#: single point: a ball, not a shape, and nothing a reader learns from.
#:
#: **And it is what property 8 costs.** `_settle` is exact rather than
#: approximated because it only ever sees the linked set, which was 333 of
#: 3,843 records. Counting `ran` makes that set 2,700, and the solver holds a
#: full pairwise matrix — 58 MB rebuilt 500 times, which does not finish a
#: nightly build. So this list is load-bearing twice over: it keeps the picture
#: legible AND it is why the exact solver is still affordable.
#:
#: A run that did something the graph can name still has that link, so it is
#: settled by what it FILED or PROMOTED and sits beside it. A run whose only
#: link is what it is a run of joins the band of unplaced records around its
#: own compartment, which is where it was before any of this and is honest:
#: nothing about it says where it belongs.
NOT_PLACING: frozenset[str] = frozenset({"ran"})


def _degrees(atlas: Atlas) -> dict[str, int]:
    """How connected a record is, FOR THE PURPOSE OF PLACING IT.

    Deliberately not `report.degrees`, which answers a different question —
    how connected a record is, full stop — and is what sizes a dot and ranks
    the hub list. `NOT_PLACING` is the difference and it is the whole reason
    the two are separate functions rather than one.
    """
    degree = {node_id: 0 for node_id in atlas.nodes}
    for edge in atlas.edges.values():
        if edge.type in NOT_PLACING:
            continue
        if edge.subject in degree:
            degree[edge.subject] += 1
        if edge.object in degree:
            degree[edge.object] += 1
    return degree


def pull_for(count: int) -> float:
    """How far out a record with this many connections belongs, as a share of
    the compartment radius.

    **A function of the record's OWN degree, and of nothing else.** Ranking it
    against the rest of the library was tried first and broke property 4: a
    rank is a position in a population, so one arriving record shifted every
    rank behind it and the radial force below dragged the whole map to its new
    place. Measured, that made a warm-started record move 11.5 units where a
    cold-started one moved 6.9 — the opposite of what warm starting is for.

    Logarithmic, because a hub with 1,683 links and a record with three are
    not five hundred times apart in importance. Dividing by the maximum would
    put every record but one at the rim: on the live graph the top degree is
    1,683 and the median among the linked is 1.

    `MIDDLE_AT` is where the curve bottoms out, so anything that connected is
    as central as the picture goes.
    """
    if count <= 0:
        return EDGE_PULL
    share = min(1.0, math.log2(1 + count) / math.log2(1 + MIDDLE_AT))
    return EDGE_PULL - (EDGE_PULL - CENTRE_PULL) * share


def pulls(degree: dict[str, int]) -> dict[str, float]:
    """`pull_for` over a whole graph."""
    return {node_id: pull_for(count) for node_id, count in degree.items()}


def _settle(
    ids: list[str],
    atlas: Atlas,
    warm: dict[str, tuple[float, float]],
    pull: dict[str, float],
) -> np.ndarray:
    """The forces, over the linked records only.

    Repulsion between every pair inside range, attraction along every link, and
    a gentle pull toward where the record belongs: its compartment's direction,
    at the radius its connectedness earns. Exact rather than
    approximated because the linked set is small: the whole point of laying the
    unlinked ones out separately is that this loop never sees them.
    """
    index = {node_id: i for i, node_id in enumerate(ids)}

    def placed_before(node_id: str) -> bool:
        was = warm.get(node_id)
        return was is not None and all(math.isfinite(value) for value in was)

    def start(node_id: str) -> tuple[float, float]:
        """Where a record begins this pass: where it ended the last one, or a
        place derived from its id.

        A warm position is a HINT and it comes from a file, so a value that is
        not a finite number is not one. Measured: a single corrupt coordinate
        in the previous map turned all 333 linked records into `NaN` inside one
        pass, because every one of them is subtracted from every other. That
        writes `NaN` into the graph, which `json` accepts on the way back in and
        a browser refuses, so the map would have stayed broken for good with
        nothing saying why. A record whose hint is unusable starts where a
        record nobody has placed starts.
        """
        if placed_before(node_id):
            return warm[node_id]
        return _add(
            anchor(atlas.nodes[node_id].region, pull.get(node_id, EDGE_PULL)),
            _seeded(node_id),
        )

    points = np.array([start(node_id) for node_id in ids], dtype=np.float64)
    # How far each record may move on the first iteration. See
    # `WARM_STEP_SHARE`: a record the last build placed cools from a tenth of
    # the step, so an unchanged graph comes back as the same map instead of
    # being re-heated every night.
    ceiling = np.array(
        [
            STEP_FROM * (WARM_STEP_SHARE if placed_before(node_id) else 1.0)
            for node_id in ids
        ],
        dtype=np.float64,
    )
    anchors = np.array(
        [
            anchor(atlas.nodes[node_id].region, pull.get(node_id, EDGE_PULL))
            for node_id in ids
        ],
        dtype=np.float64,
    )
    # Both ends of every link, once, in sorted order so the accumulation below
    # adds the same numbers in the same order on every run.
    links = sorted(
        {
            (index[edge.subject], index[edge.object])
            for edge in atlas.edges.values()
            if edge.subject in index
            and edge.object in index
            and edge.subject != edge.object
        }
    )
    if not links:
        return points
    left = np.array([a for a, _ in links])
    right = np.array([b for _, b in links])

    for step in range(ITERATIONS):
        delta = points[:, None, :] - points[None, :, :]
        distance = np.sqrt((delta**2).sum(axis=2))
        np.fill_diagonal(distance, np.inf)
        # A floor, so two records the build placed on the same spot push apart
        # instead of dividing by zero and leaving the map.
        near = np.maximum(distance, 1e-6)
        strength = np.where(distance < REPULSION_RANGE, REST**2 / near**2, 0.0)
        force = (delta / near[:, :, None] * strength[:, :, None]).sum(axis=1)

        span = points[right] - points[left]
        length = np.maximum(np.sqrt((span**2).sum(axis=1)), 1e-6)
        pull = (span / length[:, None]) * (length / REST)[:, None]
        np.add.at(force, left, pull)
        np.add.at(force, right, -pull)

        force += (anchors - points) * ANCHOR_PULL

        # Cooling: how far anything may move falls to nothing, so the last
        # passes settle rather than keep swapping two records round.
        limit = ceiling * (1.0 - step / ITERATIONS)
        moved = np.sqrt((force**2).sum(axis=1))
        scale = np.minimum(1.0, limit / np.maximum(moved, 1e-9))
        points += force * scale[:, None]
    return points


def _add(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    return a[0] + b[0], a[1] + b[1]


def _fans(
    atlas: Atlas, placed: dict[str, tuple[float, float]]
) -> dict[str, list[str]]:
    """`{a placed record: the records that hang off it and nothing else}`.

    A follower is a record every one of whose connections goes to ONE record
    the forces have already placed. That is the whole test, and it is
    deliberately strict: a record connected to two placed records has a
    position that is a compromise between them, which is a question for the
    solver and not for a ring.

    Walked over every edge, `NOT_PLACING` included, because those are exactly
    the links this exists for: they are kept out of the solver so an exact
    pairwise pass stays affordable, and keeping them out of the PICTURE'S
    arithmetic too is what left a hub sitting away from its own fan.

    Sorted, so the same graph gives the same picture.
    """
    beside: dict[str, set[str]] = {}
    for edge in atlas.edges.values():
        if edge.subject not in atlas.nodes or edge.object not in atlas.nodes:
            continue
        if edge.subject == edge.object:
            continue
        beside.setdefault(edge.subject, set()).add(edge.object)
        beside.setdefault(edge.object, set()).add(edge.subject)

    fans: dict[str, list[str]] = {}
    for node_id, peers in beside.items():
        if node_id in placed or len(peers) != 1:
            continue
        host = next(iter(peers))
        if host not in placed:
            continue
        fans.setdefault(host, []).append(node_id)
    return {host: sorted(followers) for host, followers in sorted(fans.items())}


def compute(
    atlas: Atlas,
    warm: dict[str, tuple[float, float]] | None = None,
) -> dict[str, tuple[float, float]]:
    """A position for every node, and nothing else.

    `warm` is the previous build's positions. Passing them is what keeps a map
    a person learnt from being redrawn because one record arrived; leaving them
    out gives a picture settled from nothing, which is the same picture every
    time for the same graph but not the same picture as the pass before.
    """
    if not atlas.nodes:
        return {}
    started = dict(warm or {})
    degree = _degrees(atlas)
    linked = sorted(node_id for node_id, count in degree.items() if count > 0)
    loose = sorted(node_id for node_id, count in degree.items() if count == 0)

    # **Two degrees, and they answer two questions.** `_degrees` skips
    # `NOT_PLACING` and decides which records the forces see, because a link
    # joining thousands of records to one says nothing about where they sit.
    # How far out a record is pulled is a different question — how connected is
    # it, full stop — and it has to be the same count `report.degrees` gives,
    # because that is what sizes the dot on the canvas.
    #
    # Ranking the pull by the placing degree instead was tried and is visibly
    # wrong: `capability:capture` has 1,683 records pointing at it and three
    # links the forces see, so the biggest dot on the picture was placed at the
    # rim. A reader cannot be shown the most connected thing in the library as
    # the furthest one out.
    pull = pulls(report.degrees(atlas))

    placed: dict[str, tuple[float, float]] = {}
    if linked:
        settled = _settle(linked, atlas, started, pull)
        for node_id, point in zip(linked, settled):
            placed[node_id] = (float(point[0]), float(point[1]))

    # **A leaf sits around the record it points at.** This is property 6 for
    # the records the forces never see: `NOT_PLACING` keeps a run's `ran` link
    # out of the solver, so before this the link was DRAWN and used to place
    # nothing, and a hub with 1,674 of them settled on the far side of the map
    # from its own fan. The operator's screenshot of exactly that is what this
    # is written against.
    #
    # It is not a force and does not need to be. A record whose only
    # connection is to one other record has one answer to where it belongs,
    # and arithmetic gives it: a disc around that record, area-uniform so the
    # fan fills rather than crowding its inside edge, stepped by the golden
    # angle so it does not fall into spokes.
    fans = _fans(atlas, placed)
    for host, followers in fans.items():
        middle = placed[host]
        spread = LEAF_SPREAD * math.sqrt(len(followers))
        for position, node_id in enumerate(followers):
            share = (position + 0.5) / len(followers)
            radius = spread * math.sqrt(share)
            angle = 2.0 * math.pi * ((position * _GOLDEN_STEP) % 1.0)
            placed[node_id] = (
                middle[0] + radius * math.cos(angle),
                middle[1] + radius * math.sin(angle),
            )

    # How far everything placed so far reaches from the MIDDLE OF THE MAP, per
    # compartment. What the band of records connected to nothing at all has to
    # sit outside of.
    reach: dict[Region, float] = {region: 0.0 for region in _ORDER}
    for node_id, (x, y) in placed.items():
        region = atlas.nodes[node_id].region
        reach[region] = max(reach[region], math.hypot(x, y))

    by_region: dict[Region, list[str]] = {region: [] for region in _ORDER}
    for node_id in loose:
        if node_id in placed:
            continue
        by_region[atlas.nodes[node_id].region].append(node_id)

    for region, node_ids in by_region.items():
        if not node_ids:
            continue
        start, width = sector(region)
        # Clear of what is connected, and never inside the compartment's own
        # full radius: a record nothing links to is the least connected thing
        # there is, and the picture is read from the middle out.
        inner = max(reach[region] + LOOSE_GAP, ANCHOR_RADIUS)
        outer = inner + LOOSE_MAX
        total = len(node_ids)
        # The compartment's own wedge, held slightly clear of its neighbours so
        # two rims do not run into each other where they meet.
        span = width * LOOSE_WEDGE
        for position, node_id in enumerate(node_ids):
            # Area-uniform between the two radii, so the band fills evenly
            # instead of crowding its inside edge.
            share = (position + 0.5) / total
            radius = math.sqrt(inner**2 + (outer**2 - inner**2) * share)
            # Stepped by the golden angle WITHIN the wedge, so the rim fills
            # evenly rather than in spokes and still says which compartment
            # each record belongs to.
            angle = start + width * (1.0 - LOOSE_WEDGE) / 2.0 + span * (
                (position * _GOLDEN_STEP) % 1.0
            )
            placed[node_id] = (radius * math.cos(angle), radius * math.sin(angle))

    # One scaling at the end, so the whole map fills the disc a renderer
    # expects however far the forces happened to spread. Distances and angles
    # are untouched, which is why this is not the picture moving.
    extent = max(math.hypot(x, y) for x, y in placed.values())
    factor = OUTER_RADIUS / extent if extent > 0 else 1.0
    return {
        node_id: (round(x * factor, PRECISION), round(y * factor, PRECISION))
        for node_id, (x, y) in placed.items()
    }


__all__ = [
    "ANCHOR_RADIUS",
    "LOOSE_MAX",
    "OUTER_RADIUS",
    "PRECISION",
    "anchor",
    "compute",
    "sector",
]
