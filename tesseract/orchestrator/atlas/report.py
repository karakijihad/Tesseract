"""`ATLAS.md` — the graph in words, and every line cites what it read.

Four things, and the choice of four is the point. **Hubs** are what everything
points at. **Orphans** have nothing pointing at them, so retrieval will never
reach them. **Bridges** are the single edge joining two regions — lose one and
the library splits in half without anything failing. **Deltas** are what became
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

from tesseract.orchestrator.atlas.model import NodeKind
from tesseract.orchestrator.atlas.store import Atlas, atlas_dir

# How many rows each section prints. A report nobody finishes reading is a
# report nobody reads, and the full graph is in `atlas.json` beside it.
TOP_N = 15


def _undirected(atlas: Atlas) -> dict[str, set[str]]:
    """Adjacency over nodes that exist. A dangling reference is a finding of
    its own and must not invent the node it points at."""
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in atlas.nodes}
    for edge in atlas.edges.values():
        if edge.subject in adjacency and edge.object in adjacency:
            adjacency[edge.subject].add(edge.object)
            adjacency[edge.object].add(edge.subject)
    return adjacency


def hubs(atlas: Atlas, limit: int = TOP_N) -> list[tuple[str, int]]:
    """Ranked by degree, and a node with one connection is not a hub.

    Without the floor the section fills to its limit with leaf entities that
    are mentioned exactly once, and a list where the last ten rows all read
    "1 connection" teaches the reader to skip the section.
    """
    degree = {node_id: len(peers) for node_id, peers in _undirected(atlas).items()}
    ranked = sorted(degree.items(), key=lambda item: (-item[1], item[0]))
    return [(node_id, count) for node_id, count in ranked[:limit] if count > 1]


def orphans(atlas: Atlas) -> list[str]:
    """Nothing points here, so no walk of the graph arrives.

    Not the same as unreachable: `memory_search` finds these by their text
    like anything else. What an orphan cannot do is turn up because something
    ELSE was relevant, which is most of what a map is for.

    Entities are excluded — every entity node is created by a mention, so an
    orphaned one would be a contradiction rather than a finding.
    """
    pointed_at = {
        edge.object for edge in atlas.edges.values() if edge.object in atlas.nodes
    }
    return sorted(
        node_id
        for node_id, node in atlas.nodes.items()
        if node_id not in pointed_at and node.kind is not NodeKind.ENTITY
    )


def bridges(atlas: Atlas) -> list[tuple[str, str]]:
    """Edges whose removal splits the graph into two REGIONS — Tarjan,
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


def render(atlas: Atlas, *, prior: Atlas | None = None) -> str:
    kinds: dict[str, int] = defaultdict(int)
    for node in atlas.nodes.values():
        kinds[node.kind.value] += 1
    provenances: dict[str, int] = defaultdict(int)
    for edge in atlas.edges.values():
        provenances[edge.provenance.value] += 1

    def title(node_id: str) -> str:
        node = atlas.nodes.get(node_id)
        return f"`{node_id}`" + (f" — {node.title}" if node and node.title else "")

    built = atlas.built_at.isoformat(timespec="seconds") if atlas.built_at else "unknown"
    lines = [
        "# Atlas",
        "",
        f"Built {built} by builder version {atlas.builder_version}. "
        f"**{len(atlas.nodes)} nodes, {len(atlas.edges)} edges.** Derived from "
        "the memory store and the vault — delete it and the next pass rebuilds "
        "it.",
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

    lines += ["## Hubs", "", "What the most things point at.", ""]
    ranked = hubs(atlas)
    lines += [f"- {title(node_id)} — {count} connection(s)" for node_id, count in ranked] or [
        "- (nothing is connected to anything yet)"
    ]

    lines += ["", "## Orphans", "",
              "No connection points at these, so nothing reaches them by "
              "following links. Search still finds them by their text — the "
              "map is what cannot get there.", ""]
    alone = orphans(atlas)
    lines += [f"- {title(node_id)}" for node_id in alone[:TOP_N]] or ["- (none)"]
    if len(alone) > TOP_N:
        lines.append(f"- …and {len(alone) - TOP_N} more")

    lines += ["", "## Bridges", "",
              "The single connection joining two regions. Lose one and the "
              "library splits without anything failing.", ""]
    joins = bridges(atlas)
    lines += [f"- {title(a)} ↔ {title(b)}" for a, b in joins[:TOP_N]] or ["- (none)"]
    if len(joins) > TOP_N:
        lines.append(f"- …and {len(joins) - TOP_N} more")

    if prior is not None:
        new_nodes, new_edges = deltas(atlas, prior)
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


def write(atlas: Atlas, *, prior: Atlas | None = None, path: Path | None = None) -> Path:
    target = path or (atlas_dir() / "ATLAS.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(atlas, prior=prior), encoding="utf-8")
    return target


__all__ = [
    "TOP_N",
    "bridges",
    "dangling",
    "deltas",
    "hubs",
    "orphans",
    "render",
    "write",
]
