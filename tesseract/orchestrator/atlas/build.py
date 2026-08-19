"""One pass of the builder.

Two decisions are worth stating, because both are invariants rather than
convenience:

**The graph is rebuilt, not amended.** Each pass derives a fresh atlas and
replaces the file. A derived layer that only ever gains rows accumulates
ghosts — a memory the operator deleted keeps its edges forever, and nothing
notices, because nothing was looking for an absence. What survives from the
previous graph is exactly two things: each node's `first_seen`, so "what
became connected since last pass" stays answerable, and the content hash of a
raw file whose stamp has not moved, which is the one expensive read here.

**A version bump re-derives everything.** `BUILDER_VERSION` is bumped whenever
what the builder extracts changes. A prior graph from an older version has its
hashes discarded, so every file is read again rather than trusted. Incremental
mode alone means a better extractor only ever applies to what was written
after the update, and the library then holds two qualities of extraction with
no way to tell which is which.

The builder writes ONE place: `<TESSERACT_HOME>/atlas/`. Memory bodies and
vault bytes are inputs and stay inputs — proved by a hash comparison test, not
by this paragraph.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tesseract.orchestrator.atlas import builders, report, store
from tesseract.orchestrator.atlas.config import AtlasConfig, load_atlas_config
from tesseract.orchestrator.atlas.model import Node
from tesseract.orchestrator.atlas.store import Atlas

log = logging.getLogger(__name__)

# Bump when what the builder extracts changes — a new edge type, a changed
# locator format, a corrected provenance class. The next pass then re-derives
# everything instead of leaving old records at the old quality.
BUILDER_VERSION = 1


@dataclass(frozen=True)
class BuildReport:
    nodes: int
    edges: int
    memories: int
    pages: int
    hashes_reused: int
    full_rederive: bool
    duration_ms: float
    path: str = ""
    conflicts: int = 0
    report_path: str = ""


def derive(
    *,
    memory_store: Any,
    vault_manager: Any,
    now: datetime,
    windows,
    version: int,
    reuse: dict[str, tuple[str, str]] | None = None,
) -> tuple[Atlas, int, int]:
    """The whole graph from the inputs, touching no disk but the ones it reads.

    Split out from `run_build` so the verification pass can derive a second
    graph in memory and compare — a rebuild that had to write somewhere first
    would need a scratch tree to prove the tree it already has.
    """
    fresh = Atlas(builder_version=version, built_at=now)
    memories = builders.build_memory(
        fresh, memory_store, now=now, version=version, windows=windows
    )
    pages = builders.build_vault(
        fresh, vault_manager, now=now, version=version, windows=windows,
        reuse=reuse or {},
    )
    for conflict in builders.find_duplicate_slugs(
        memory_store, now=now, version=version
    ):
        fresh.add_conflict(conflict)
    return fresh, memories, pages


def run_build(
    *,
    memory_store: Any,
    vault_manager: Any,
    now: datetime | None = None,
    config: AtlasConfig | None = None,
    atlas_path=None,
    report_path=None,
) -> BuildReport:
    started = time.monotonic()
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cfg = config or load_atlas_config()
    windows = cfg.review_after_days

    prior = store.load(atlas_path)
    # A prior graph from a different builder is not evidence about anything —
    # its hashes were taken by code that may have read the file differently.
    reusable = prior.builder_version == BUILDER_VERSION
    reuse = (
        {n.id: (n.input_stamp, n.content_hash)
         for n in prior.nodes.values() if n.content_hash}
        if reusable else {}
    )

    fresh, memories, pages = derive(
        memory_store=memory_store,
        vault_manager=vault_manager,
        now=moment,
        windows=windows,
        version=BUILDER_VERSION,
        reuse=reuse,
    )

    _carry_first_seen(fresh, prior)
    hashes_reused = sum(
        1
        for node in fresh.nodes.values()
        if node.content_hash
        and reuse.get(node.id) == (node.input_stamp, node.content_hash)
    )
    path = store.save(fresh, atlas_path)
    # The readable half, written from the same graph in the same pass — two
    # passes could disagree, and a report that disagrees with the data beside
    # it is worse than no report.
    written = report.write(fresh, prior=prior, path=report_path)
    return BuildReport(
        nodes=len(fresh.nodes),
        edges=len(fresh.edges),
        memories=memories,
        pages=pages,
        hashes_reused=hashes_reused,
        full_rederive=not reusable,
        duration_ms=(time.monotonic() - started) * 1000.0,
        path=str(path),
        conflicts=len(fresh.conflicts),
        report_path=str(written),
    )


def _carry_first_seen(fresh: Atlas, prior: Atlas) -> int:
    """A node the graph already knew keeps the date it was first seen.

    Kept even across a version bump: when a thing entered the library is a
    fact about the library, not about the builder that read it.
    """
    carried = 0
    for node_id, node in list(fresh.nodes.items()):
        was = prior.nodes.get(node_id)
        if was is None or was.first_seen >= node.first_seen:
            continue
        fresh.nodes[node_id] = Node(
            id=node.id,
            kind=node.kind,
            title=node.title,
            locator=node.locator,
            builder_version=node.builder_version,
            first_seen=was.first_seen,
            updated_at=node.updated_at,
            content_hash=node.content_hash,
            input_stamp=node.input_stamp,
            aliases=node.aliases,
        )
        carried += 1
    return carried


__all__ = ["BUILDER_VERSION", "BuildReport", "run_build"]
