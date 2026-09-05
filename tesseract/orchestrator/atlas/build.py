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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tesseract.orchestrator.atlas import (
    agenda,
    builders,
    layout,
    made_of,
    playbooks,
    report,
    store,
    tags,
    trees,
    turns,
)
from tesseract.orchestrator.atlas.config import AtlasConfig, load_atlas_config
from tesseract.orchestrator.atlas.model import Node
from tesseract.orchestrator.atlas.store import Atlas

log = logging.getLogger(__name__)

# Bump when what the builder extracts changes — a new edge type, a changed
# locator format, a corrected provenance class. The next pass then re-derives
# everything instead of leaving old records at the old quality.
# 6: the filed-at moment of a defect now comes from the evidence body rather
# than from the minute its file name carries, so a record filed inside a
# build's own minute is drawn where a version-5 pass drew it early. Without the
# bump the next verify would compare an old graph to a new rebuild, agree on
# the version, and report the difference as drift rather than as staleness.
# 7: a raw source is hashed against the root its `source_path` was actually
# written against, through `locate.source_file`. Three producers write that
# field against three roots and each builder joined one of them, so on the
# machine this was measured on NONE of the 26 raw sources resolved and every
# one carried an empty content hash. Every graph drawn before this holds that
# empty field, which is exactly the "two qualities of extraction with no way
# to tell which is which" invariant 7 exists to stop.
# 8: the map reaches four compartments instead of two and a bit. A record is
# named by what it IS and no name is longer than `model.MAX_TITLE_CHARS` (a
# filed defect was named with the log line it was found in, a median of 98
# characters); the derived tree layer and the diary reach the graph, which was
# a fifth of the memory store before; a tag that enough records share is a node,
# which is the only hub-shaped connector the store already writes; the agenda is
# drawn, so what the machine INTENDED sits beside what it did; and what it is
# made of — the entries and stages that declare everything that runs on its own
# — fills the fourth compartment, which had been declared and empty since it was
# written. Every node re-derives.
BUILDER_VERSION = 8


@dataclass(frozen=True)
class BuildReport:
    nodes: int
    edges: int
    memories: int
    pages: int
    hashes_reused: int
    full_rederive: bool
    duration_ms: float
    runs: int = 0
    feedback: int = 0
    path: str = ""
    conflicts: int = 0
    report_path: str = ""
    trees: int = 0
    diary: int = 0
    tags: int = 0
    agenda: int = 0
    capabilities: int = 0
    playbooks: int = 0
    # Keyed by the region's own value, so a caller reads it without importing
    # the enum. Every region appears.
    regions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Derived:
    """What one pass read, per builder, beside the graph it read it into.

    A dataclass and not a tuple. It was a tuple of five and there are nine
    builders now: `graph, memories, pages, ran, filed` was already a line
    every caller had to keep in the right order, and a sixth position would
    have been silently mis-bound by the first caller that forgot one.
    """

    graph: Atlas
    memories: int = 0
    pages: int = 0
    runs: int = 0
    feedback: int = 0
    trees: int = 0
    diary: int = 0
    tags: int = 0
    agenda: int = 0
    capabilities: int = 0
    playbooks: int = 0


def derive(
    *,
    memory_store: Any,
    vault_manager: Any,
    now: datetime,
    windows,
    version: int,
    runs_within_days: int,
    reuse: dict[str, tuple[str, str]] | None = None,
    runs: Any = None,
    evidence_dir: Any = None,
    agenda_root: Any = None,
    turns_root: Any = None,
    skills_dir: Any = None,
) -> Derived:
    """The whole graph from the inputs, touching no disk but the ones it reads.

    Split out from `run_build` so the verification pass can derive a second
    graph in memory and compare — a rebuild that had to write somewhere first
    would need a scratch tree to prove the tree it already has.
    """
    fresh = Atlas(builder_version=version, built_at=now)
    # What every record in the store is filed under, collected as the readers
    # walk and decided once at the end. Which labels earn a node depends on
    # the whole distribution, so no reader that sees one record at a time can
    # answer it. `tags.py` is the rule.
    filings = tags.Ledger()
    memories = builders.build_memory(
        fresh, memory_store, now=now, version=version, windows=windows,
        ledger=filings,
    )
    # The rest of the store, which is most of it. `build_memory` asks
    # `MemoryStore` for every TYPED record and gets 119 of 647 files; the
    # derived tree layer and the diary are the other four fifths of what the
    # operator actually reads. `trees.py` says which of them are drawn and why
    # the daily layer is not.
    grouped, reflections, subjects = trees.build_trees(
        fresh,
        memory_store.store_dir,
        now=now,
        version=version,
        windows=windows,
        reuse=reuse or {},
        ledger=filings,
    )
    pages = builders.build_vault(
        fresh, vault_manager, now=now, version=version, windows=windows,
        reuse=reuse or {},
    )
    # Asked of the watchman rather than spelled out here, so the writer and the
    # reader cannot end up pointing at two trees. Imported at the call rather
    # than at the top, which is what `_run_rows` does with the scheduler and
    # for the same reason: nothing that merely imports the atlas should drag
    # the watchman in with it.
    if evidence_dir is None:
        from tesseract.orchestrator.watchman.report import evidence_dir as judged_in

        judged_tree = judged_in()
    else:
        judged_tree = evidence_dir
    filed = builders.build_feedback(
        fresh,
        judged_tree,
        now=now,
        version=version,
        windows=windows,
        within_days=runs_within_days,
        # An evidence file is never rewritten, so an unmoved stamp is the end
        # of the question and the tree is not re-hashed every pass.
        reuse=reuse or {},
    )
    # Last, deliberately: every edge a run draws points AT a node one of the
    # others made — a memory it promoted, a defect it filed, an agenda item it
    # acted on, the capability that declares it — and each is drawn only when
    # that node is already in the graph. The ordering is the mechanism, not
    # tidiness, and a test pins it.
    # What the machine is MADE OF, before the body, because a run points AT
    # the capability that declares it and the edge is drawn only when that
    # capability is already in the graph.
    parts = made_of.build_made_of(
        fresh, now=now, version=version, windows=windows
    )
    # What it DECIDED to do, also before the body, for the same reason: a run
    # that published an item points at it.
    decided = agenda.build_agenda(
        fresh,
        agenda_root if agenda_root is not None else _agenda_root(),
        now=now,
        version=version,
        windows=windows,
        within_days=runs_within_days,
        reuse=reuse or {},
    )
    rows = _run_rows() if runs is None else list(runs)
    ran = builders.build_runs(
        fresh,
        rows,
        now=now,
        version=version,
        windows=windows,
        within_days=runs_within_days,
    )
    # The other half of what the body did: what a person asked for. A turn is
    # a run too, and it is the one record that names the task it worked, so
    # it comes after the agenda for the reason the run builder does.
    ran += turns.build_turns(
        fresh,
        turns_root if turns_root is not None else _turns_root(),
        now=now,
        version=version,
        windows=windows,
        within_days=runs_within_days,
    )
    # What it learned to DO, after the turns, because a playbook points at the
    # turns it was learned from and the edge is drawn only when they are
    # already in the graph.
    learned = playbooks.build_playbooks(
        fresh,
        skills_dir if skills_dir is not None else _skills_dir(),
        now=now,
        version=version,
        windows=windows,
    )
    # After every builder that mints an entity, and only where one exists.
    # `link_subjects` says why: 464 topic trees name a subject and 10 of those
    # subjects are things the library already knows, so drawing all of them
    # would have added 454 nodes joined to one record each.
    trees.link_subjects(
        fresh, subjects, now=now, version=version, windows=windows
    )
    labels, _left_out = tags.build_tags(
        fresh, filings, now=now, version=version, windows=windows
    )
    for conflict in builders.find_duplicate_slugs(
        memory_store, now=now, version=version
    ):
        fresh.add_conflict(conflict)
    # The picture is settled by `run_build` and not here. It needs the previous
    # build's positions to start from, which is what stops one arriving record
    # from redrawing a map somebody had learnt, and `prior` is only read one
    # level up. Verification derives a graph to compare against the file and
    # never looks at where anything sits, so it no longer pays for a layout it
    # throws away.
    return Derived(
        graph=fresh,
        memories=memories,
        pages=pages,
        runs=ran,
        feedback=filed,
        trees=grouped,
        diary=reflections,
        tags=labels,
        agenda=decided,
        capabilities=parts,
        playbooks=learned,
    )


def _skills_dir():
    from tesseract.paths import workspace_dir

    return workspace_dir() / "skills"


def _turns_root():
    from tesseract.orchestrator.turns import turns_root

    return turns_root()


def _agenda_root():
    """Where the agenda lives, asked of the store that owns it.

    Imported at the call, the same as the watchman's evidence tree and the run
    log, so nothing that merely imports the atlas drags the autonomy kernel in
    with it.
    """
    from tesseract.orchestrator.autonomy.paths import agenda_root

    return agenda_root()


def _run_rows() -> list[dict]:
    """Every row of the run log, or none.

    Fail-soft on purpose, and it is the one place in this module that is. A
    fresh install has no run log and a machine whose log is mid-write is not a
    reason to leave the operator with no atlas at all: the body is one of four
    compartments and the report names what it could not reach.
    """
    from tesseract.scheduler.log import iter_runs, runs_path

    try:
        return list(iter_runs(runs_path()))
    except OSError:
        log.warning("atlas: the run log could not be read; the body is not drawn")
        return []


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

    made = derive(
        memory_store=memory_store,
        vault_manager=vault_manager,
        now=moment,
        windows=windows,
        version=BUILDER_VERSION,
        runs_within_days=cfg.body.runs_within_days,
        reuse=reuse,
    )
    fresh = made.graph

    _carry_first_seen(fresh, prior)
    # Last, because a position is a fact about the finished graph, and warm
    # started from the last one, because a force layout settled from nothing
    # lands somewhere else every time the graph changes at all. A record that
    # was already on the map begins where it ended.
    fresh.positions = layout.compute(fresh, warm=prior.positions)
    # What this pass added, compared once and written into the graph. The
    # readable report and the graph surface both answer "what is new since the
    # last pass" and the previous graph is about to be overwritten, so the
    # comparison has to happen here and be recorded, not re-run by whoever
    # asks later.
    new_nodes, new_edges = report.deltas(fresh, prior)
    fresh.delta = store.Delta(nodes=tuple(new_nodes), edges=tuple(new_edges))
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
    written = report.write(fresh, path=report_path)
    return BuildReport(
        nodes=len(fresh.nodes),
        edges=len(fresh.edges),
        memories=made.memories,
        pages=made.pages,
        runs=made.runs,
        feedback=made.feedback,
        trees=made.trees,
        diary=made.diary,
        tags=made.tags,
        agenda=made.agenda,
        capabilities=made.capabilities,
        playbooks=made.playbooks,
        hashes_reused=hashes_reused,
        full_rederive=not reusable,
        duration_ms=(time.monotonic() - started) * 1000.0,
        path=str(path),
        conflicts=len(fresh.conflicts),
        report_path=str(written),
        regions={
            region.value: count
            for region, count in fresh.counts_by_region().items()
        },
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
        fresh.nodes[node_id] = node.with_first_seen(was.first_seen)
        carried += 1
    return carried


__all__ = ["BUILDER_VERSION", "BuildReport", "Derived", "run_build"]
