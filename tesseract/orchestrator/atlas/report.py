"""`ATLAS.md` — the graph in words, and every line cites what it read.

Four things, and the choice of four is the point. **Hubs** are what everything
points at. **Orphans** have nothing pointing at them, so retrieval will never
reach them. **Bridges** are the single edge whose removal disconnects two halves of the
graph. Lose one and the library splits without anything failing. (A `Region`
is a different thing: which compartment of the brain a node belongs to.) **Deltas** are what became
connected since the last pass.

What is deliberately absent is community detection. At this corpus size it
names clusters already known, its membership re-shuffles on every rebuild so
the churn is not information, and it is the one structural claim that cannot
cite its own reasoning. Everything below can: a hub is a degree count, an
orphan is an empty inbound set, a bridge is an edge whose removal disconnects
its endpoints, and each is printed with the ids behind it.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from tesseract.orchestrator.atlas.model import NodeKind, Region, label_of
from tesseract.orchestrator.atlas.store import Atlas, atlas_dir

# How many rows each section prints. A report nobody finishes reading is a
# report nobody reads, and the full graph is in `atlas.json` beside it.
TOP_N = 15


# What this machine holds that the graph does not reach, and why. Named out
# loud because a map that quietly stops at the edge of what it indexed reads
# as a map of everything: the operator would take an empty region for an idle
# one, which is the same fault the Autonomy panel's liveness contract exists
# to stop. A tree leaves this list in the pass that gives it a builder.
NOT_INDEXED: tuple[tuple[str, str], ...] = (
    (
        "most of what a run read, wrote and refused",
        "most jobs record how MANY rather than which, so nothing here can "
        "name them. Two say which: the watchman names the sources it read, and "
        "the sealing pass names the seals it wrote and the source trees it "
        "appended to. The rest is a change at each recorder rather than one a "
        "reader can make",
    ),
    (
        "what the watchman was looking at",
        "it reads the same set of sources on every pass, so a link per pass "
        "would say what the job name already says, and would leave a handful "
        "of places that everything points at. Which sources a pass read is on "
        "that pass's own record, where the run opens",
    ),
    (
        "what a filed defect was judging",
        "the record names its source and kind, not the row or run node it is "
        "about, so the only link drawn is to the run that filed it",
    ),
    (
        "the daily notes and the daily briefs",
        "they are a timeline rather than things to look up, and a daily note "
        "already reaches the map wherever a memory cites it as its source, so "
        "drawing them again would put one file here twice under two names",
    ),
    (
        "a tag fewer than three records carry, or more than a tenth of them do",
        "one shared label is a coincidence and a label on most of the library "
        "joins everything to everything, which is the hairball this map opens "
        "by refusing",
    ),
    (
        "which scheduled run carried out a decision",
        "a conversation turn that takes up a task records which, and the map "
        "draws that; a scheduled run records what it published and never "
        "which item it acted on, so those two still sit side by side unjoined",
    ),
    (
        "the autonomy kernel's own loops",
        "nine of the thirty things that write a run row are not in the "
        "manifest or the pipeline, so those runs cannot say which part of the "
        "machine they are a run of",
    ),
    ("sessions", "no builder reads the chat record yet"),
    ("skills and agents", "no builder reads the workshop or the roster yet"),
    (
        "the runtime's own logs, ledgers and archives",
        "what the observer wrote, what the backend wrote, what the drift check "
        "wrote, what was approved, what it cost, and the copies a finished lane "
        "or worker left behind. Each is a stream or a running account rather "
        "than a thing to look up, and the map reaches one through the record "
        "that cites it. Drawing them is how a map that fits on a screen becomes "
        "a log viewer",
    ),
)


# Every tree the runtime keeps state in, and which of the two things the map
# does with it. The keys are `retention.yaml`'s own `trees`, and that is the
# point: a tree with a retention window is a tree something WRITES, declared by
# the runtime rather than by a list kept here and forgotten.
#
# The agenda sat unread for two months while the surface said the map covered
# the runtime, so the map was quietly missing a tree rather than openly. These
# two maps make that impossible to repeat: a new state tree fails a test until
# somebody says which of the two it is, and there is no third answer.
DRAWN_BY: dict[str, str] = {
    "scheduler_runs": "builders.build_runs",
    "turn_manifests": "turns.build_turns",
    "watchman_reports": "builders.build_feedback",
    "agenda_records": "agenda.build_agenda",
}

#: The rest, each pointing at the `NOT_INDEXED` entry that says so out loud.
DECLARED_AS: dict[str, str] = {
    "sessions": "sessions",
    "observer_logs": "the runtime's own logs, ledgers and archives",
    "backend_logs": "the runtime's own logs, ledgers and archives",
    "conscience": "the runtime's own logs, ledgers and archives",
    "lane_archives": "the runtime's own logs, ledgers and archives",
    "worker_archives": "the runtime's own logs, ledgers and archives",
    "approvals_ledger": "the runtime's own logs, ledgers and archives",
    "usage_ledger": "the runtime's own logs, ledgers and archives",
}


# Kinds for which "nothing points here" is not a finding. An orphan is a node
# retrieval can never reach by following links; an entity is reached by name,
# and a run and the defects it filed are reached by when they happened, so
# listing every quiet night would bury the memories that really are
# unreachable.
_NOT_ORPHANABLE = frozenset({NodeKind.ENTITY, NodeKind.RUN, NodeKind.FEEDBACK})


def _undirected(atlas: Atlas) -> dict[str, set[str]]:
    """Adjacency over nodes that exist. A dangling reference is a finding of
    its own and must not invent the node it points at."""
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in atlas.nodes}
    for edge in atlas.edges.values():
        if edge.subject in adjacency and edge.object in adjacency:
            adjacency[edge.subject].add(edge.object)
            adjacency[edge.object].add(edge.subject)
    return adjacency


def degrees(atlas: Atlas) -> dict[str, int]:
    """How many other records each one is connected to, every node named.

    The one count of a node's connectedness. `hubs` ranks by it and the graph
    surface sizes a dot by it, so a big dot on the picture and a row in the
    hub list are the same claim. It counts distinct neighbours that EXIST: an
    edge into nothing is a finding of its own (`dangling`) and must not also
    make the record it left look better connected than it is.
    """
    return {node_id: len(peers) for node_id, peers in _undirected(atlas).items()}


def hubs(atlas: Atlas, limit: int = TOP_N) -> list[tuple[str, int]]:
    """Ranked by degree, and a node with one connection is not a hub.

    Without the floor the section fills to its limit with leaf entities that
    are mentioned exactly once, and a list where the last ten rows all read
    "1 connection" teaches the reader to skip the section.
    """
    ranked = sorted(degrees(atlas).items(), key=lambda item: (-item[1], item[0]))
    return [(node_id, count) for node_id, count in ranked[:limit] if count > 1]


def orphans(atlas: Atlas) -> list[str]:
    """Nothing touches these, so no walk of the graph arrives.

    Not the same as unreachable: `memory_search` finds these by their text
    like anything else. What an orphan cannot do is turn up because something
    ELSE was relevant, which is most of what a map is for.

    **No connection at all, in either direction.** It used to be no INBOUND
    connection, and that was a claim the retrieval it describes does not make:
    `retrieval.retrieve` builds its adjacency both ways, because an edge is
    evidence of a connection whichever end the question started from. So a
    record that points at three things and is pointed at by none is reached
    from any of the three, and calling it unreachable was wrong.

    It was wrong quietly until the derived tree layer arrived. A tree points
    at its tags and its subject and nothing points back, so 483 perfectly
    reachable records became 483 orphans in one pass — and `relink.py`, which
    reads this list to decide what to repair, would have set about repairing
    them.

    Entities are excluded — every entity node is created by a mention, so an
    orphaned one would be a contradiction rather than a finding.
    """
    touching = degrees(atlas)
    return sorted(
        node_id
        for node_id, node in atlas.nodes.items()
        if not touching.get(node_id) and node.kind not in _NOT_ORPHANABLE
    )


def bridges(atlas: Atlas) -> list[tuple[str, str]]:
    """Edges whose removal splits the graph into two HALVES — Tarjan,
    iteratively, with pendant edges filtered out.

    Every edge to a leaf is a bridge by the textbook definition, and saying
    so is useless: cutting it strands one node, which the orphan section
    already covers. What this section promises is "lose it and the library
    splits", so both sides have to be more than a single record.

    Iterative rather than recursive: a library where one topic accumulated
    for a year is a long chain, and that is exactly the shape that overflows
    a recursive walk.
    """
    adjacency = _undirected(atlas)
    discovery: dict[str, int] = {}
    low: dict[str, int] = {}
    found: list[tuple[str, str]] = []
    counter = 0

    for root in sorted(adjacency):
        if root in discovery:
            continue
        # (node, parent, iterator over its neighbours)
        stack: list[tuple[str, str | None, list[str]]] = [
            (root, None, sorted(adjacency[root]))
        ]
        discovery[root] = low[root] = counter
        counter += 1
        while stack:
            node, parent, pending = stack[-1]
            if pending:
                peer = pending.pop()
                if peer == parent:
                    continue
                if peer in discovery:
                    low[node] = min(low[node], discovery[peer])
                    continue
                discovery[peer] = low[peer] = counter
                counter += 1
                stack.append((peer, node, sorted(adjacency[peer])))
                continue
            stack.pop()
            if parent is not None:
                low[parent] = min(low[parent], low[node])
                if low[node] > discovery[parent]:
                    found.append(tuple(sorted((parent, node))))  # type: ignore[arg-type]
    return sorted(
        pair for pair in set(found)
        if len(adjacency[pair[0]]) > 1 and len(adjacency[pair[1]]) > 1
    )


def dangling(atlas: Atlas) -> list[tuple[str, str]]:
    """Edges pointing at ids no node carries — a link into nothing.

    Not always a defect in the atlas: a `backlinks_from` naming a page that
    was deleted is a stale record in the vault, and this is where it becomes
    visible. Each row names the edge's locator, so the fix is one file away.
    """
    out: list[tuple[str, str]] = []
    for edge in atlas.edges.values():
        for side in (edge.subject, edge.object):
            if side not in atlas.nodes:
                out.append((side, edge.locator))
    return sorted(set(out))


def deltas(atlas: Atlas, prior: Atlas) -> tuple[list[str], list[str]]:
    """What is here that was not, in nodes and in edges."""
    new_nodes = sorted(set(atlas.nodes) - set(prior.nodes))
    new_edges = sorted(set(atlas.edges) - set(prior.edges))
    return new_nodes, new_edges


def render(atlas: Atlas) -> str:
    """The graph in words. What changed since the last pass is READ off the
    graph rather than recomputed here: `run_build` records it with
    `deltas()`, and the surface that draws the same set reads the same
    field, so the page and the picture cannot disagree about what is new."""
    kinds: dict[str, int] = defaultdict(int)
    for node in atlas.nodes.values():
        kinds[node.kind.value] += 1
    provenances: dict[str, int] = defaultdict(int)
    for edge in atlas.edges.values():
        provenances[edge.provenance.value] += 1
    region_counts = atlas.counts_by_region()

    def title(node_id: str) -> str:
        node = atlas.nodes.get(node_id)
        return f"`{node_id}`" + (f" — {node.title}" if node and node.title else "")

    built = atlas.built_at.isoformat(timespec="seconds") if atlas.built_at else "unknown"
    lines = [
        "# Atlas",
        "",
        f"Built {built} by builder version {atlas.builder_version}. "
        f"**{len(atlas.nodes)} nodes, {len(atlas.edges)} edges.** Derived from "
        "the memory store, the vault, the run log, the defects the watchman "
        "filed, the agenda and what the runtime declares it is made of — "
        "delete it and the next pass rebuilds it.",
        "",
        "| Nodes | | Edges by origin | |",
        "| --- | --- | --- | --- |",
    ]
    node_rows = sorted(kinds.items())
    edge_rows = sorted(provenances.items())
    for index in range(max(len(node_rows), len(edge_rows))):
        left = f"`{node_rows[index][0]}` | {node_rows[index][1]}" if index < len(node_rows) else " | "
        right = f"`{edge_rows[index][0]}` | {edge_rows[index][1]}" if index < len(edge_rows) else " | "
        lines.append(f"| {left} | {right} |")
    lines.append("")

    lines += [
        "## Compartments",
        "",
        "Which half of the brain each thing belongs to. A compartment with "
        "nothing in it is a compartment nothing builds yet, not an empty one.",
        "",
        "| Compartment | Nodes |",
        "| --- | --- |",
    ]
    for region in Region:
        lines.append(f"| {label_of(region)} | {region_counts.get(region, 0)} |")
    lines.append("")

    lines += [
        "## Not in the graph",
        "",
        "What this machine holds that nothing here reaches. Retrieval cannot "
        "find it and no line above counts it.",
        "",
    ]
    lines += [f"- **{what}** — {why}" for what, why in NOT_INDEXED]
    lines.append("")

    lines += ["## Hubs", "", "What the most things point at.", ""]
    ranked = hubs(atlas)
    lines += [f"- {title(node_id)} — {count} connection(s)" for node_id, count in ranked] or [
        "- (nothing is connected to anything yet)"
    ]

    lines += ["", "## Orphans", "",
              "Nothing connects to these in either direction, so nothing "
              "reaches them by following links. Search still finds them by "
              "their text — the map is what cannot get there.", ""]
    alone = orphans(atlas)
    lines += [f"- {title(node_id)}" for node_id in alone[:TOP_N]] or ["- (none)"]
    if len(alone) > TOP_N:
        lines.append(f"- …and {len(alone) - TOP_N} more")

    lines += ["", "## Bridges", "",
              "The single connection joining two halves of the graph. Lose "
              "one and the library splits without anything failing. This is "
              "not about compartments: both ends of a bridge can be in the "
              "same one.", ""]
    joins = bridges(atlas)
    lines += [f"- {title(a)} ↔ {title(b)}" for a, b in joins[:TOP_N]] or ["- (none)"]
    if len(joins) > TOP_N:
        lines.append(f"- …and {len(joins) - TOP_N} more")

    if atlas.delta is not None:
        new_nodes = list(atlas.delta.nodes)
        new_edges = list(atlas.delta.edges)
        lines += ["", "## Since the last pass", "",
                  f"{len(new_nodes)} new node(s), {len(new_edges)} new edge(s).", ""]
        lines += [f"- {title(node_id)}" for node_id in new_nodes[:TOP_N]]
        if len(new_nodes) > TOP_N:
            lines.append(f"- …and {len(new_nodes) - TOP_N} more")

    loose = dangling(atlas)
    if loose:
        lines += ["", "## Links into nothing", "",
                  "An edge naming an id no record carries. Usually a stale "
                  "reference in the source rather than a fault in the map.", ""]
        lines += [f"- `{target}` — cited by `{locator}`" for target, locator in loose[:TOP_N]]
        if len(loose) > TOP_N:
            lines.append(f"- …and {len(loose) - TOP_N} more")

    if atlas.conflicts:
        lines += ["", "## Conflicts", "",
                  "Recorded, not resolved. Both sides stand until you pick.", ""]
        for conflict in sorted(atlas.conflicts.values(), key=lambda c: c.id):
            lines.append(f"- **{conflict.kind}** — {conflict.detail}")

    return "\n".join(lines).rstrip() + "\n"


def write(atlas: Atlas, *, path: Path | None = None) -> Path:
    target = path or (atlas_dir() / "ATLAS.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(atlas), encoding="utf-8")
    return target


__all__ = [
    "NOT_INDEXED",
    "TOP_N",
    "bridges",
    "dangling",
    "degrees",
    "deltas",
    "hubs",
    "orphans",
    "render",
    "write",
]
