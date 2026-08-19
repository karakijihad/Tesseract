"""One way to ask the atlas a question, whoever is asking.

Invariant 9: the assistant, its sub-agents and MCP clients query through the
same surface with the same filters — provenance threshold, temporal window,
scope, expansion depth, token budget — and get citations back with the
results. Two retrieval paths that happen to share a disk are two knowledge
systems, and the one an MCP client sees drifts from the one the assistant
sees without either noticing.

**Seeding is injected, expansion is not.** Finding the first few relevant
records is what `memory_search` and `vault_search` are already good at, and
re-implementing that here would create the third path this exists to prevent.
What the atlas adds is what those two cannot do: walk the declared edges from
there, filter by how the connection was made, and return the locator of every
hop so a caller can decline any of it.

The default seed is deliberately weak — a literal match over titles and
aliases. It exists so the contract is usable and testable without a running
index, not as a retriever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

from tesseract.orchestrator.atlas.model import PRECEDENCE, Edge, Node, NodeKind, Provenance
from tesseract.orchestrator.atlas.store import Atlas

# Rough characters-per-token. Deliberately pessimistic: a budget that is
# occasionally under-spent costs a caller nothing, and one that is over-spent
# costs them a truncated turn they cannot see coming.
CHARS_PER_TOKEN = 4

_RANK = {provenance: index for index, provenance in enumerate(PRECEDENCE)}


@dataclass(frozen=True)
class Citation:
    """One hop, and where to read it."""

    subject: str
    object: str
    type: str
    provenance: Provenance
    locator: str
    asserted_by: str = ""

    def as_json(self) -> dict:
        return {
            "from": self.subject,
            "to": self.object,
            "type": self.type,
            "provenance": self.provenance.value,
            "locator": self.locator,
            "asserted_by": self.asserted_by,
        }

    def as_line(self) -> str:
        by = f" ({self.asserted_by})" if self.asserted_by else ""
        return (
            f"{self.subject} —{self.type}→ {self.object} "
            f"[{self.provenance.value}{by} · {self.locator}]"
        )


@dataclass(frozen=True)
class Hit:
    node: Node
    depth: int
    citations: tuple[Citation, ...] = ()

    @property
    def cost(self) -> int:
        text = self.node.title + self.node.locator + "".join(
            c.as_line() for c in self.citations
        )
        return max(1, len(text) // CHARS_PER_TOKEN)

    def as_json(self) -> dict:
        return {
            "id": self.node.id,
            "kind": self.node.kind.value,
            "title": self.node.title,
            "locator": self.node.locator,
            "depth": self.depth,
            "citations": [c.as_json() for c in self.citations],
        }


@dataclass(frozen=True)
class Query:
    """Every filter the contract carries. A caller that ignores one gets the
    default, and the default is stated here rather than at each call site."""

    text: str = ""
    seeds: tuple[str, ...] = ()
    # The floor, in precedence order. `operator_asserted` accepts only what
    # the operator wrote; `inferred_by_model` (the default) accepts everything
    # and lets the citations say which is which.
    min_provenance: Provenance = Provenance.INFERRED_BY_MODEL
    since: datetime | None = None
    until: datetime | None = None
    kinds: tuple[NodeKind, ...] = ()
    max_depth: int = 1
    token_budget: int = 2000
    limit: int = 20

    def __post_init__(self) -> None:
        if self.max_depth < 0:
            raise ValueError("max_depth cannot be negative")
        if self.token_budget < 1 or self.limit < 1:
            raise ValueError("a budget of nothing returns nothing; ask for at least 1")


@dataclass(frozen=True)
class Result:
    hits: tuple[Hit, ...] = ()
    seeds: tuple[str, ...] = ()
    truncated: str = ""
    tokens_spent: int = 0
    filtered_edges: int = 0

    def as_json(self) -> dict:
        return {
            "hits": [h.as_json() for h in self.hits],
            "seeds": list(self.seeds),
            "truncated": self.truncated,
            "tokens_spent": self.tokens_spent,
            "filtered_edges": self.filtered_edges,
        }

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(h.node.id for h in self.hits)


SeedFn = Callable[[Atlas, Query], Iterable[str]]


def literal_seeds(atlas: Atlas, query: Query) -> list[str]:
    """Title and alias matching, and nothing cleverer.

    A caller with the memory pipeline or the vault index passes its own seed
    function; this one exists so the contract has a default that works with no
    embeddings, no network and no backend.
    """
    needle = query.text.strip().casefold()
    if not needle:
        return []
    found = [
        node.id
        for node in atlas.nodes.values()
        if needle in node.title.casefold()
        or needle in node.id.casefold()
        or any(needle in alias.casefold() for alias in node.aliases)
    ]
    return sorted(found)


def _passes(edge: Edge, query: Query) -> bool:
    if _RANK[edge.provenance] > _RANK[query.min_provenance]:
        return False
    if query.since is not None and edge.asserted_at < query.since:
        return False
    if query.until is not None and edge.asserted_at > query.until:
        return False
    return True


def _in_scope(node: Node, query: Query) -> bool:
    return not query.kinds or node.kind in query.kinds


def retrieve(
    atlas: Atlas,
    query: Query,
    *,
    seed_fn: SeedFn = literal_seeds,
) -> Result:
    """Seed, expand, cite, and stop when the budget says so.

    Breadth-first and sorted at every step, so two callers asking the same
    question of the same graph get the same answer in the same order — which
    is the whole claim the shared contract makes.
    """
    seeds = [
        node_id
        for node_id in dict.fromkeys([*query.seeds, *seed_fn(atlas, query)])
        if node_id in atlas.nodes
    ]

    # Adjacency built once per call, both directions: an edge is evidence of a
    # connection whichever end the question started from.
    outgoing: dict[str, list[tuple[str, Edge]]] = {}
    filtered = 0
    for edge in atlas.edges.values():
        if not _passes(edge, query):
            filtered += 1
            continue
        outgoing.setdefault(edge.subject, []).append((edge.object, edge))
        outgoing.setdefault(edge.object, []).append((edge.subject, edge))

    hits: list[Hit] = []
    spent = 0
    truncated = ""
    seen: set[str] = set()
    frontier: list[tuple[str, int, tuple[Citation, ...]]] = [
        (node_id, 0, ()) for node_id in seeds
    ]

    while frontier:
        node_id, depth, trail = frontier.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        node = atlas.nodes.get(node_id)
        if node is None:
            continue

        if _in_scope(node, query):
            hit = Hit(node=node, depth=depth, citations=trail)
            if len(hits) >= query.limit:
                truncated = f"stopped at the {query.limit}-result limit"
                break
            if spent + hit.cost > query.token_budget:
                truncated = (
                    f"stopped at the {query.token_budget}-token budget after "
                    f"{len(hits)} result(s)"
                )
                break
            hits.append(hit)
            spent += hit.cost

        if depth >= query.max_depth:
            continue
        for peer, edge in sorted(outgoing.get(node_id, ()), key=lambda item: item[0]):
            if peer in seen:
                continue
            frontier.append((peer, depth + 1, (*trail, _cite(edge))))

    return Result(
        hits=tuple(hits),
        seeds=tuple(seeds),
        truncated=truncated,
        tokens_spent=spent,
        filtered_edges=filtered,
    )


def _cite(edge: Edge) -> Citation:
    return Citation(
        subject=edge.subject,
        object=edge.object,
        type=edge.type,
        provenance=edge.provenance,
        locator=edge.locator,
        asserted_by=edge.asserted_by,
    )


def render(result: Result) -> str:
    """The answer as text, citations included — never as a footnote a caller
    has to ask for. An uncited claim from a derived graph is the thing this
    whole phase exists to stop producing."""
    if not result.hits:
        return "Nothing in the atlas matched.\n"
    lines: list[str] = []
    for hit in result.hits:
        prefix = "· " * hit.depth
        lines.append(f"{prefix}{hit.node.title or hit.node.id}  ({hit.node.locator})")
        for citation in hit.citations:
            lines.append(f"{prefix}    via {citation.as_line()}")
    if result.truncated:
        lines.append(f"\n[{result.truncated}]")
    return "\n".join(lines) + "\n"


__all__ = [
    "CHARS_PER_TOKEN",
    "Citation",
    "Hit",
    "Query",
    "Result",
    "SeedFn",
    "literal_seeds",
    "render",
    "retrieve",
]
