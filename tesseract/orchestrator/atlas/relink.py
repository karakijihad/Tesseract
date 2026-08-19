"""Repair what the map found: memories nothing can reach by following links.

The atlas reports orphans; this acts on them, and the trigger is the finding
rather than a clock. It is a stage of the same nightly pass, declared to read
what the builder wrote — so it runs when there is an atlas to read, and never
because it is a particular time.

**Why this is not part of the builder.** The builder writes derived artifacts
only, which is what makes a bad build cost one rebuild instead of data. This
writes memory frontmatter. Putting the repair inside the builder would trade
away the guarantee that makes the whole atlas safe to keep rewriting.

What it repairs is specific. Every orphan measured on this machine had zero
`auto_links` AND zero entities, while its siblings from the same writer had
five links each — the signature of a write that happened while embeddings were
unreachable, since `auto_link` returns `embeddings_unavailable` and nothing
ever came back for it. `AutoLinker.rebuild_auto_links` existed for this and
was called by nothing.

An orphan with genuinely no neighbours above threshold is retried on later
passes, deliberately: the store grows, and a memory with no relative today can
have one next week. The cost of being wrong about that is one vector search.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from tesseract.orchestrator.atlas import report
from tesseract.orchestrator.atlas.model import NodeKind
from tesseract.orchestrator.atlas.store import Atlas

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelinkReport:
    linked: int = 0
    attempted: int = 0
    # Orphans the linker declined, keyed by the reason it gave. `no_neighbors`
    # is a healthy answer; `embeddings_unavailable` is the whole cause this
    # exists to repair, and the two must not read the same.
    declined: dict[str, int] = None  # type: ignore[assignment]
    skipped_over_cap: int = 0

    def __post_init__(self) -> None:
        if self.declined is None:
            object.__setattr__(self, "declined", {})

    @property
    def blocked_on_embeddings(self) -> bool:
        return bool(self.declined.get("embeddings_unavailable"))


def orphan_memories(atlas: Atlas) -> list[str]:
    """The atlas's own orphan list, narrowed to memories and unprefixed.

    Reads `report.orphans` rather than recomputing: two definitions of orphan
    is how the repair comes to disagree with the report that triggered it.
    """
    out: list[str] = []
    for node_id in report.orphans(atlas):
        node = atlas.nodes.get(node_id)
        if node is not None and node.kind is NodeKind.MEMORY:
            out.append(node_id.removeprefix("mem:"))
    return out


async def relink_orphans(
    atlas: Atlas,
    *,
    store: Any,
    linker: Any,
    max_per_run: int,
) -> RelinkReport:
    """Re-run the auto-linker over each orphaned memory, in order, capped.

    Sequential on purpose. `_add_auto_links` is a read-modify-write of a
    memory's frontmatter and each pass also writes its chosen neighbours, so
    two concurrent repairs touching one neighbour would lose an edge — the
    parallelism would buy milliseconds and cost the thing being repaired.
    """
    orphans = orphan_memories(atlas)
    attempted = 0
    linked = 0
    declined: dict[str, int] = {}

    for memory_id in orphans[:max_per_run]:
        record = store.read(memory_id, log_access=False)
        if record is None:
            declined["not_found"] = declined.get("not_found", 0) + 1
            continue
        _fm, body = record
        attempted += 1
        try:
            result = await linker.auto_link(memory_id, body)
        except Exception:  # noqa: BLE001 — one bad record must not end the pass
            log.exception("relink: auto_link raised for %s", memory_id)
            declined["raised"] = declined.get("raised", 0) + 1
            continue
        if result.status == "ok":
            linked += 1
            continue
        reason = result.reason or "declined"
        declined[reason] = declined.get(reason, 0) + 1
        if reason == "embeddings_unavailable":
            # Every remaining orphan will say the same thing. Stopping is the
            # difference between one honest report and fifty identical ones.
            break

    return RelinkReport(
        linked=linked,
        attempted=attempted,
        declined=declined,
        skipped_over_cap=max(0, len(orphans) - max_per_run),
    )


__all__ = ["RelinkReport", "orphan_memories", "relink_orphans"]
