"""Is the atlas on disk the atlas its inputs imply?

Invariant 7 says the derived layer is rebuildable, and that rebuildability is
proved rather than claimed — a guarantee never exercised is how a derived tree
quietly becomes primary. So a job rebuilds into a scratch file and compares.

What is compared is the STRUCTURE, not the bytes. Every build stamps
`asserted_at` and `updated_at` with the moment it ran, so a byte comparison
would report drift on every pass and mean nothing. Drift is: a node or edge
that should exist and does not, one that exists and should not, or one whose
identity, provenance, locator or content hash differs. Those are the fields a
reader trusts; a timestamp is not one of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tesseract.orchestrator.atlas.store import Atlas


def node_signature(atlas: Atlas) -> dict[str, tuple]:
    return {
        node.id: (node.kind.value, node.title, node.locator, node.content_hash,
                  tuple(node.aliases))
        for node in atlas.nodes.values()
    }


def edge_signature(atlas: Atlas) -> dict[str, tuple]:
    return {
        edge.id: (edge.subject, edge.object, edge.type, edge.provenance.value,
                  edge.creator.value, edge.locator, edge.review_after_days,
                  edge.asserted_by)
        for edge in atlas.edges.values()
    }


@dataclass(frozen=True)
class Drift:
    """What the rebuild found. Each list names ids, so a report can say which
    artifact rather than how many."""

    missing: tuple[str, ...] = ()   # in the rebuild, absent from the live file
    extra: tuple[str, ...] = ()     # in the live file, absent from the rebuild
    changed: tuple[str, ...] = ()   # in both, differing
    kinds: dict[str, str] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (self.missing or self.extra or self.changed)

    @property
    def count(self) -> int:
        return len(self.missing) + len(self.extra) + len(self.changed)

    def describe(self) -> str:
        if self.clean:
            return "no drift"
        parts = []
        for label, ids in (("missing", self.missing), ("extra", self.extra),
                           ("changed", self.changed)):
            if ids:
                shown = ", ".join(ids[:5]) + (f" …+{len(ids) - 5}" if len(ids) > 5 else "")
                parts.append(f"{len(ids)} {label} ({shown})")
        return "; ".join(parts)


def compare(live: Atlas, rebuilt: Atlas) -> Drift:
    """`live` is what is on disk; `rebuilt` is what the inputs imply now."""
    missing: list[str] = []
    extra: list[str] = []
    changed: list[str] = []
    kinds: dict[str, str] = {}

    for label, left, right in (
        ("node", node_signature(live), node_signature(rebuilt)),
        ("edge", edge_signature(live), edge_signature(rebuilt)),
    ):
        for key in sorted(set(right) - set(left)):
            missing.append(key)
            kinds[key] = label
        for key in sorted(set(left) - set(right)):
            extra.append(key)
            kinds[key] = label
        for key in sorted(set(left) & set(right)):
            if left[key] != right[key]:
                changed.append(key)
                kinds[key] = label

    live_conflicts = set(live.conflicts)
    rebuilt_conflicts = set(rebuilt.conflicts)
    for key in sorted(rebuilt_conflicts - live_conflicts):
        missing.append(key)
        kinds[key] = "conflict"
    for key in sorted(live_conflicts - rebuilt_conflicts):
        extra.append(key)
        kinds[key] = "conflict"

    return Drift(
        missing=tuple(missing),
        extra=tuple(extra),
        changed=tuple(changed),
        kinds=kinds,
    )


__all__ = ["Drift", "compare", "edge_signature", "node_signature"]
