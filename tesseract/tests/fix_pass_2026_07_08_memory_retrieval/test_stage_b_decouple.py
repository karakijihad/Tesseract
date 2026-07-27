"""RC1 regression — P7 live-gate diagnosis (memory-retrieval-diagnosis.md).

Stage A's `stage_a_prefilter` scores candidates by literal word-set
overlap (`title.lower().split()`), then used to be the ONLY thing Stage
B's BM25/vector search were allowed to see (`filter_ids=candidate_ids`).
A query whose token doesn't equal a title token verbatim — e.g.
"gate_fizz" vs the title token "gate_fizz.py" (the ".py" suffix breaks
set equality) — never made Stage A's top list, so the memory was never
even handed to FTS5/FAISS, regardless of how well it would have matched
there.

Fix: Stage B's BM25 and vector routes search the FULL index, unfiltered.
Stage A's candidates are merged into the scoring context (temporal decay
/ confidence / expiry) rather than gating what Stage B can find.

Covers:
- the exact live repro: "gate_fizz" query vs title "gate_fizz.py
  verified for P7 gate" — misses Stage A, must still hit via retrieve().
- a second punctuated-title shape (short stem query).
- an unrelated query still returns no hybrid hits (RC1 didn't turn
  Stage B into "return everything").
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from tesseract.memory.fts_index import FTSIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.retrieval import RetrievalPipeline
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


def _bundle(tmp_path: Path) -> tuple[MemoryStore, MemoryIndex, FTSIndex, RetrievalPipeline]:
    store_dir = tmp_path / "memory-store"
    (store_dir / "derived").mkdir(parents=True, exist_ok=True)
    store = MemoryStore(store_dir=store_dir)
    index = MemoryIndex(store_dir=store_dir)
    fts = FTSIndex(db_path=store_dir / "derived" / "fts.db")
    pipeline = RetrievalPipeline(store=store, index=index, embeddings=None, fts_index=fts)
    return store, index, fts, pipeline


def _seed(
    store: MemoryStore,
    fts: FTSIndex,
    *,
    mem_id: str,
    title: str,
    body: str,
    importance: int = 6,
    mem_type: MemoryType = MemoryType.PROJECT,
) -> MemoryFrontmatter:
    now = datetime.now(timezone.utc)
    fm = MemoryFrontmatter(
        id=mem_id,
        type=mem_type,
        title=title,
        summary=body[:80],
        tags=[],
        entities=[],
        importance=importance,
        created_at=now,
        updated_at=now,
    )
    assert store.write(fm, body)
    fts.add(fm.id, fm.title, body)
    return fm


def _seed_decoys(store: MemoryStore, fts: FTSIndex, n: int = 30) -> None:
    """Seed `n` unrelated, higher-importance memories so Stage A's
    top-30 literal-overlap cap genuinely excludes a zero-overlap target —
    reproducing the diagnosis's real 417-memory corpus, where the target
    record's Stage-A score never makes the cut. Importance 10 (vs. the
    target's default 6) guarantees each decoy outranks a zero-overlap
    target regardless of recency."""
    for i in range(n):
        _seed(
            store, fts,
            mem_id=f"mem_decoy_{i}",
            title=f"Unrelated topic number {i}",
            body=f"Decoy record {i} about an entirely unrelated subject, used only to fill Stage A's candidate cap.",
            importance=10,
        )


def test_gate_fizz_bare_token_query_now_reaches_stage_b(tmp_path: Path) -> None:
    """Live-gate repro: 'gate_fizz' vs title 'gate_fizz.py verified for
    P7 gate' must miss Stage A's literal overlap but still hit via
    retrieve() once Stage B searches the full FTS index."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed_decoys(store, fts)
    _seed(
        store, fts,
        mem_id="mem_5e36f3a2",
        title="gate_fizz.py verified for P7 gate",
        body="Confirmed gate_fizz.py passes the P7 live gate checks end to end with no regressions found.",
    )

    # Confirms the RC1 root cause is still live at Stage A: the bare
    # token never equals the punctuated title token via word-set overlap,
    # and 30 higher-importance decoys fill the top-30 cap ahead of it.
    candidates = pipeline.stage_a_prefilter("gate_fizz")
    assert "mem_5e36f3a2" not in {fm.id for fm in candidates}

    packet = asyncio.run(pipeline.retrieve("gate_fizz"))
    assert "mem_5e36f3a2" in {r.memory_id for r in packet.results}, (
        "RC1: bare-stem query must reach Stage B's full-index BM25 search"
    )


def test_punctuated_title_short_stem_query_reaches_stage_b(tmp_path: Path) -> None:
    """Second punctuated-title shape: a short stem query against a
    filename-style title token."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed_decoys(store, fts)
    _seed(
        store, fts,
        mem_id="mem_foo_verified",
        title="foo.py verified",
        body="The foo module configuration script passed every verification check without any failures.",
    )

    candidates = pipeline.stage_a_prefilter("foo")
    assert "mem_foo_verified" not in {fm.id for fm in candidates}

    packet = asyncio.run(pipeline.retrieve("foo"))
    assert "mem_foo_verified" in {r.memory_id for r in packet.results}


def test_unrelated_query_returns_no_hybrid_hits(tmp_path: Path) -> None:
    """Decoupling Stage B from Stage A's gate must not turn full-index
    search into 'return everything' — a truly unrelated query still
    scores zero hybrid hits."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_5e36f3a2",
        title="gate_fizz.py verified for P7 gate",
        body="Confirmed gate_fizz.py passes the P7 live gate checks end to end with no regressions found.",
    )

    candidates = pipeline.stage_a_prefilter("banana pancake recipe")
    results = asyncio.run(
        pipeline.stage_b_hybrid_search("banana pancake recipe", None, candidates)
    )
    assert results == []


def test_type_filter_still_excludes_wrong_type_hit_from_full_index(tmp_path: Path) -> None:
    """Review finding: the old `candidate_ids` gate used to make Stage A's
    `type_filter` scoping implicitly apply to Stage B too (a wrong-type
    memory was never in Stage A's candidates, so it was never in the
    filter set). Once Stage B searches the full FTS/vector index
    unfiltered, a wrong-type memory can be found directly by BM25 and
    must still be dropped — `type_filter` has to be re-checked inside
    `stage_b_hybrid_search` itself now."""
    store, _index, fts, pipeline = _bundle(tmp_path)
    _seed(
        store, fts,
        mem_id="mem_project_marker",
        title="unique_marker_zzz project note",
        body="A project-typed record carrying the shared unique_marker_zzz token for BM25 matching.",
        mem_type=MemoryType.PROJECT,
    )
    _seed(
        store, fts,
        mem_id="mem_reference_marker",
        title="unique_marker_zzz reference note",
        body="A reference-typed record carrying the same unique_marker_zzz token for BM25 matching.",
        mem_type=MemoryType.REFERENCE,
    )

    packet = asyncio.run(pipeline.retrieve("unique_marker_zzz", type_filter=MemoryType.PROJECT))
    ids = {r.memory_id for r in packet.results}
    assert "mem_project_marker" in ids
    assert "mem_reference_marker" not in ids
