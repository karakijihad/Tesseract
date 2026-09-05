"""`<TESSERACT_HOME>/atlas/atlas.json` — the whole derived graph, on disk.

One file, atomically replaced. The atlas is small (nodes are memories, wiki
pages, raw sources and entities — thousands, not millions) and it is derived,
so the simplest storage that cannot half-write is the right one. No database:
that would move the source of truth off the files that are the source of truth.

The previous graph is read back before every build so an incremental pass can
answer two questions about each node — has its input changed, and was it
derived by this builder — without re-reading the corpus it came from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tesseract.orchestrator.atlas.model import Conflict, Edge, Node, Region


def atlas_dir() -> Path:
    from tesseract.paths import home_dir

    return home_dir() / "atlas"


def atlas_path() -> Path:
    return atlas_dir() / "atlas.json"


@dataclass(frozen=True)
class Delta:
    """What became part of the graph in the pass that drew it.

    Written into the file rather than recomputed, because the two readers of
    it — `ATLAS.md` and the graph surface — have to agree, and the only way
    they can is by reading one recorded answer. Recomputing on the surface
    would need the previous graph, which the pass overwrote.

    Absent is not empty. A graph drawn before this was recorded, or by
    something that did not record it, carries `None`, and a reader says what
    changed is not known rather than that nothing did.
    """

    nodes: tuple[str, ...] = ()
    edges: tuple[str, ...] = ()


@dataclass
class Atlas:
    """Nodes and edges, keyed by id, plus what produced them."""

    builder_version: int = 0
    built_at: datetime | None = None
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)
    conflicts: dict[str, Conflict] = field(default_factory=dict)
    # What this build added over the one before it. Set by `run_build` from
    # the one comparison, `report.deltas`.
    delta: Delta | None = None
    # Where each node sits, settled at build time by `layout.py` and written
    # beside the nodes rather than into a file of its own. It is a pure
    # function of the graph, so keeping it here is what stops the two from ever
    # describing different pictures.
    positions: dict[str, tuple[float, float]] = field(default_factory=dict)

    def add_node(self, node: Node) -> Node:
        """First sighting wins `first_seen`; everything else is this build's.

        A node re-derived from a changed file is the same node — losing its
        first-seen date would make the graph look newer every time anything
        was edited, and "what became connected since the last pass" is one of
        the four things this thing is for.
        """
        existing = self.nodes.get(node.id)
        if existing is not None:
            node = node.with_first_seen(existing.first_seen)
        self.nodes[node.id] = node
        # A position is a fact about the finished graph, so a graph that gains
        # a node no longer has one. Cheap during a build, where the map is
        # empty until the last step; the point is that a later builder cannot
        # leave a node the surface has nowhere to draw.
        self.positions.clear()
        return node

    def add_edge(self, edge: Edge) -> Edge:
        self.edges[edge.id] = edge
        return edge

    def add_conflict(self, conflict: Conflict) -> Conflict:
        self.conflicts[conflict.id] = conflict
        return conflict

    def counts_by_region(self) -> dict[Region, int]:
        """How many nodes are in each compartment, every compartment named.

        A region with no nodes reports zero rather than being absent: the
        surface has to be able to say the body is not in the graph yet, and a
        missing key reads as a region nobody thought of.
        """
        counts = {region: 0 for region in Region}
        for node in self.nodes.values():
            counts[node.region] += 1
        return counts

    def edges_from(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges.values() if e.subject == node_id]

    def as_json(self) -> dict[str, Any]:
        return {
            "builder_version": self.builder_version,
            "built_at": self.built_at.isoformat() if self.built_at else None,
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "conflict_count": len(self.conflicts),
            "region_counts": {
                region.value: count for region, count in self.counts_by_region().items()
            },
            "nodes": [n.as_json() for n in sorted(self.nodes.values(), key=lambda n: n.id)],
            "edges": [e.as_json() for e in sorted(self.edges.values(), key=lambda e: e.id)],
            "conflicts": [
                c.as_json() for c in sorted(self.conflicts.values(), key=lambda c: c.id)
            ],
            "positions": {
                node_id: [x, y]
                for node_id, (x, y) in sorted(self.positions.items())
            },
            "delta": (
                None
                if self.delta is None
                else {
                    "nodes": list(self.delta.nodes),
                    "edges": list(self.delta.edges),
                }
            ),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Atlas":
        built = raw.get("built_at")
        return cls(
            builder_version=int(raw.get("builder_version") or 0),
            built_at=datetime.fromisoformat(built) if built else None,
            nodes={n["id"]: Node.from_json(n) for n in raw.get("nodes") or ()},
            edges={e["id"]: Edge.from_json(e) for e in raw.get("edges") or ()},
            conflicts={
                c["id"]: Conflict.from_json(c) for c in raw.get("conflicts") or ()
            },
            positions={
                str(node_id): (float(pair[0]), float(pair[1]))
                for node_id, pair in (raw.get("positions") or {}).items()
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            },
            delta=(
                None
                if not isinstance(raw.get("delta"), dict)
                else Delta(
                    nodes=tuple(str(i) for i in raw["delta"].get("nodes") or ()),
                    edges=tuple(str(i) for i in raw["delta"].get("edges") or ()),
                )
            ),
        )


def load(path: Path | None = None) -> Atlas:
    """The graph as last written, or an empty one. A corrupt file is an empty
    graph and a full rebuild — the atlas is derived, so the cheapest correct
    answer to "this is unreadable" is to derive it again."""
    target = path or atlas_path()
    if not target.exists():
        return Atlas()
    try:
        return Atlas.from_json(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return Atlas()


def save(atlas: Atlas, path: Path | None = None) -> Path:
    target = path or atlas_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(atlas.as_json(), indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


__all__ = ["Atlas", "Delta", "atlas_dir", "atlas_path", "load", "save"]
