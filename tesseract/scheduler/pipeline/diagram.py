"""The declared graph, in a shape something can draw.

`graph.py` answers what the RUNNER needs: the order, and which failures
cascade. This answers what a READER needs — the same declaration with its
prose attached and its edges named, so a picture of it can be generated rather
than drawn.

It exists because there are two renderers and neither may derive this for
itself. The Guide's table and the floor plan's diagram describe one graph; a
second derivation is a second thing to keep in step, which is the defect that
put `STAGE_BLURBS` in a build script in the first place.

**Nothing here decides anything.** Every field is read from a declaration that
already existed: the nodes from `execution_order`, the edges from `upstreams`
and `Stage.after`, the prose from `Stage.summary` and the row's manifest entry.
An edge that no declaration carries is not in the output, which is what lets a
renderer draw every edge it is given without asking whether it is true.

Deliberately absent: cadence. When a row fires is `schedule.yaml`'s answer and
the operator's to change, so it belongs to whichever renderer is reading a
config tree — the Guide reads the shipped one, the app reads theirs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tesseract.scheduler.pipeline.graph import execution_order, producers, upstreams
from tesseract.scheduler.pipeline.registry import Row, rows
from tesseract.scheduler.pipeline.stage import Stage


class EdgeKind(str, Enum):
    """Why one stage precedes another, which is not one question.

    The difference is load-bearing rather than cosmetic and a renderer must
    show it: a `reads` edge cascades — its target is skipped when its source
    does not succeed — and an `after` edge only orders. Drawing both as one
    arrow would claim a dependency the runner does not have, and the reason
    most of the nightly set declares `after` is precisely that it reads
    canonical files rather than each other's output.
    """

    READS = "reads"
    AFTER = "after"
    IMPORTS = "imports"


@dataclass(frozen=True)
class DiagramNode:
    name: str
    summary: str
    row: str
    kind: str
    cadence: str
    budget_seconds: float
    per_day: bool
    # Position in `execution_order` itself, 1-based. Not a layout hint: it is
    # the order the runner will actually use, and a renderer that lays stages
    # out in any other sequence is drawing something that does not happen.
    order: int
    # How many edges deep this sits — the longest path from any stage with
    # nothing before it. Ranks, not pixels, because two renderers draw this at
    # two widths: the Guide on a fixed page, the app in a panel the operator
    # resizes. Both turn a rank into geometry their own way and still agree on
    # the shape. Only SAME-ROW edges count; a cross-row import cannot set rank,
    # because the rows fire apart and neither waits for the other.
    rank: int = 0
    # Position within the rank, by execution order. Two nodes of one rank have
    # no ordering between them — this only makes the drawing deterministic.
    lane: int = 0


@dataclass(frozen=True)
class DiagramEdge:
    source: str
    target: str
    kind: EdgeKind
    # What flows, for the two kinds that carry data. `None` on an `after`
    # edge, because nothing does — that is the whole distinction.
    artifact: str | None = None
    # The row `source` belongs to. Differs from the target's row only on an
    # `imports` edge, and that is what makes a cross-row arrow drawable.
    source_row: str = ""


@dataclass(frozen=True)
class RowDiagram:
    name: str
    summary: str
    why: str
    imports: tuple[str, ...]
    nodes: tuple[DiagramNode, ...]
    edges: tuple[DiagramEdge, ...]

    def incoming(self, node: str) -> tuple[DiagramEdge, ...]:
        """Every edge that must resolve before `node` runs, source-sorted."""
        return tuple(
            sorted(
                (edge for edge in self.edges if edge.target == node),
                key=lambda e: (e.source_row != self.name, e.source),
            )
        )


def _node(stage: Stage, row: str, order: int, rank: int, lane: int) -> DiagramNode:
    return DiagramNode(
        name=stage.name,
        summary=stage.summary,
        row=row,
        kind=stage.kind.value,
        cadence=stage.cadence.value,
        budget_seconds=stage.budget_seconds,
        per_day=stage.per_day,
        order=order,
        rank=rank,
        lane=lane,
    )


def row_diagram(row: Row, *, others: tuple[Row, ...] = ()) -> RowDiagram:
    """One row's declared graph. `others` supplies the cross-row producers.

    Called without `others`, an imported artifact simply has no arrow — the
    row still draws, and nothing invents a source it was not given.
    """
    from tesseract.scheduler.manifest import entry as manifest_entry

    ordered = execution_order(row.stages, external_reads=row.external_reads)
    data = upstreams(row.stages)
    owned = producers(row.stages)
    outside = {
        artifact: (other.name, owner)
        for other in others
        if other.name != row.name
        for artifact, owners in producers(other.stages).items()
        for owner in owners[:1]
    }

    edges: list[DiagramEdge] = []
    for stage in ordered:
        for artifact in stage.reads:
            for owner in owned.get(artifact, ()):
                if owner != stage.name:
                    edges.append(DiagramEdge(
                        source=owner, target=stage.name, kind=EdgeKind.READS,
                        artifact=artifact, source_row=row.name,
                    ))
            if artifact in owned:
                continue
            found = outside.get(artifact)
            if found is not None:
                source_row, owner = found
                edges.append(DiagramEdge(
                    source=owner, target=stage.name, kind=EdgeKind.IMPORTS,
                    artifact=artifact, source_row=source_row,
                ))
        # `after` is only an edge where it is not ALSO a data dependency —
        # a stage that both reads from and runs after another is one arrow,
        # and it is the cascading one.
        for earlier in stage.after:
            if earlier in data[stage.name]:
                continue
            edges.append(DiagramEdge(
                source=earlier, target=stage.name, kind=EdgeKind.AFTER,
                source_row=row.name,
            ))

    same_row: dict[str, list[str]] = {}
    for edge in edges:
        if edge.source_row == row.name:
            same_row.setdefault(edge.target, []).append(edge.source)
    # `ordered` is topological, so every dependency has a rank before it is read.
    rank: dict[str, int] = {}
    for stage in ordered:
        rank[stage.name] = 1 + max(
            (rank[dep] for dep in same_row.get(stage.name, ())), default=-1
        )
    lane: dict[str, int] = {}
    for stage in ordered:
        r = rank[stage.name]
        lane[stage.name] = sum(1 for n, v in lane.items() if rank[n] == r)

    card = manifest_entry(row.name)
    return RowDiagram(
        name=row.name,
        summary=card.summary if card else "",
        why=card.why if card else "",
        imports=tuple(row.imports),
        nodes=tuple(
            _node(s, row.name, i, rank[s.name], lane[s.name])
            for i, s in enumerate(ordered, start=1)
        ),
        edges=tuple(edges),
    )


def diagrams() -> tuple[RowDiagram, ...]:
    """Every registered row, each knowing what it imports from the others."""
    registered = rows()
    return tuple(row_diagram(r, others=registered) for r in registered)


__all__ = [
    "DiagramEdge",
    "DiagramNode",
    "EdgeKind",
    "RowDiagram",
    "diagrams",
    "row_diagram",
]
