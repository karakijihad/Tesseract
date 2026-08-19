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

from tesseract.orchestrator.atlas.model import Conflict, Edge, Node


def atlas_dir() -> Path:
    from tesseract.paths import home_dir

    return home_dir() / "atlas"


def atlas_path() -> Path:
    return atlas_dir() / "atlas.json"


@dataclass
class Atlas:
    """Nodes and edges, keyed by id, plus what produced them."""

    builder_version: int = 0
    built_at: datetime | None = None
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)
    conflicts: dict[str, Conflict] = field(default_factory=dict)

    def add_node(self, node: Node) -> Node:
        """First sighting wins `first_seen`; everything else is this build's.

        A node re-derived from a changed file is the same node — losing its
        first-seen date would make the graph look newer every time anything
        was edited, and "what became connected since the last pass" is one of
        the four things this thing is for.
        """
        existing = self.nodes.get(node.id)
        if existing is not None:
            node = Node(
                id=node.id,
                kind=node.kind,
                title=node.title,
                locator=node.locator,
                builder_version=node.builder_version,
                first_seen=existing.first_seen,
                updated_at=node.updated_at,
                content_hash=node.content_hash,
                input_stamp=node.input_stamp,
                aliases=node.aliases,
            )
        self.nodes[node.id] = node
        return node

    def add_edge(self, edge: Edge) -> Edge:
        self.edges[edge.id] = edge
        return edge

    def add_conflict(self, conflict: Conflict) -> Conflict:
        self.conflicts[conflict.id] = conflict
        return conflict

    def edges_from(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges.values() if e.subject == node_id]

    def as_json(self) -> dict[str, Any]:
        return {
            "builder_version": self.builder_version,
            "built_at": self.built_at.isoformat() if self.built_at else None,
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "conflict_count": len(self.conflicts),
            "nodes": [n.as_json() for n in sorted(self.nodes.values(), key=lambda n: n.id)],
            "edges": [e.as_json() for e in sorted(self.edges.values(), key=lambda e: e.id)],
            "conflicts": [
                c.as_json() for c in sorted(self.conflicts.values(), key=lambda c: c.id)
            ],
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


__all__ = ["Atlas", "atlas_dir", "atlas_path", "load", "save"]
