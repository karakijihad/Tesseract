"""AU-16 S1 — end-to-end test of the three leaf-lifecycle jobs."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.memory.leaf_buffers import LeafBuffer, buffer_path, source_slug
from tesseract.memory.leaf_seals import seals_root
from tesseract.memory.leaves import (
    LeafState,
    LeafStore,
    MemoryLeaf,
    mint_leaf_id,
)
from tesseract.scheduler.tasks.leaf_append import AppendBufferJob
from tesseract.scheduler.tasks.leaf_extract import (
    ExtractChunkJob,
    extract_entities,
    normalise_body,
    score_importance,
)
from tesseract.scheduler.tasks.leaf_seal import SealJob
from tesseract.scheduler.types import JobContext


def _ctx(**config) -> JobContext:
    return JobContext(job_name="test", config=dict(config))


def _make_leaf(
    body: str,
    source: str = "chat:s1",
) -> MemoryLeaf:
    now = datetime.now(timezone.utc)
    return MemoryLeaf(
        id=mint_leaf_id(),
        source=source,
        created_at=now,
        updated_at=now,
        body=body,
    )


# ---- pure helpers ---------------------------------------------------


def test_normalise_body_collapses_whitespace() -> None:
    assert normalise_body("hello   world  \n  another   line\n\n") == (
        "hello world\n another line"
    )


def test_extract_entities_dedups_and_preserves_order() -> None:
    body = "see [[alpha]] then [[beta]] again [[alpha]] and [[gamma]]"
    assert extract_entities(body) == ["alpha", "beta", "gamma"]


def test_score_importance_bounded_1_to_10() -> None:
    short_no_ents = score_importance("tiny.", entities=[])
    long_with_ents = score_importance("x" * 900, entities=["foo"])
    assert 1 <= short_no_ents <= 10
    assert 1 <= long_with_ents <= 10
    assert long_with_ents > short_no_ents


# ---- ExtractChunkJob ------------------------------------------------


async def test_extract_drops_too_short_leaves(isolated_home: Path) -> None:
    store = LeafStore()
    leaf = _make_leaf("hi")  # < MIN_LEAF_CHARS
    store.add(leaf)
    result = await ExtractChunkJob().run(_ctx())
    assert result.ok
    assert result.payload["dropped"] == 1
    assert result.payload["admitted"] == 0
    refetched = store.get(leaf.id)
    assert refetched is not None
    assert refetched.state is LeafState.DROPPED


async def test_extract_admits_with_entities_and_importance(
    isolated_home: Path,
) -> None:
    store = LeafStore()
    body = (
        "Operator highlighted [[ProjectX]] today — we should follow up on "
        "the [[OpenHuman]] comparison and discuss the long tail of feedback "
        "rows that crossed the importance floor last week."
    )
    leaf = _make_leaf(body)
    store.add(leaf)
    result = await ExtractChunkJob().run(_ctx())
    assert result.ok
    assert result.payload["admitted"] == 1
    refetched = store.get(leaf.id)
    assert refetched is not None
    assert refetched.state is LeafState.ADMITTED
    assert set(refetched.entities) == {"ProjectX", "OpenHuman"}
    assert refetched.importance >= 5


async def test_extract_skips_already_admitted_leaves(isolated_home: Path) -> None:
    store = LeafStore()
    seed = _make_leaf("plenty of body content to admit cleanly here.")
    store.add(seed)
    seed.transition_to(LeafState.ADMITTED, reason="manual")
    store.save(seed)
    result = await ExtractChunkJob().run(_ctx())
    assert result.payload["processed"] == 0


# ---- AppendBufferJob ------------------------------------------------


async def test_append_buffer_writes_id_and_transitions(isolated_home: Path) -> None:
    store = LeafStore()
    body = "Long enough body content to clear the floor easily for admission."
    leaf = _make_leaf(body, source="chat:abc")
    store.add(leaf)
    await ExtractChunkJob().run(_ctx())

    result = await AppendBufferJob().run(_ctx())
    assert result.ok
    assert result.payload["appended"] == 1
    buf = LeafBuffer("chat:abc")
    assert leaf.id in buf.read_ids()
    refetched = store.get(leaf.id)
    assert refetched is not None
    assert refetched.state is LeafState.BUFFERED


# ---- SealJob --------------------------------------------------------


async def test_seal_fires_when_count_threshold_met(isolated_home: Path) -> None:
    store = LeafStore()
    body = "Plenty of body to admit. " * 4
    for _ in range(3):
        store.add(_make_leaf(body, source="chat:abc"))
    await ExtractChunkJob().run(_ctx())
    await AppendBufferJob().run(_ctx())

    result = await SealJob().run(_ctx(max_buffer_leaves=3, max_buffer_age_seconds=999999))
    assert result.ok
    assert result.payload["seals_written"] == 1
    assert result.payload["leaves_sealed"] == 3
    seal_files = list(seals_root().glob("seal_*.json"))
    assert len(seal_files) == 1
    for leaf in store.iter_active():
        # No active leaves left for this source.
        assert leaf.source != "chat:abc"


async def test_seal_fires_on_age_threshold_even_below_count(
    isolated_home: Path,
) -> None:
    import os

    store = LeafStore()
    leaf = _make_leaf("Sufficient body content for admission here.", source="chat:age")
    store.add(leaf)
    await ExtractChunkJob().run(_ctx())
    await AppendBufferJob().run(_ctx())

    buf = LeafBuffer("chat:age")
    past = time.time() - 7200
    os.utime(buf.path, (past, past))

    result = await SealJob().run(
        _ctx(max_buffer_leaves=999, max_buffer_age_seconds=3600)
    )
    assert result.payload["seals_written"] == 1
    refetched = store.get(leaf.id)
    assert refetched is not None
    assert refetched.state is LeafState.SEALED
    assert refetched.sealed_into is not None


async def test_seal_skips_buffer_below_thresholds(isolated_home: Path) -> None:
    store = LeafStore()
    leaf = _make_leaf("Sufficient body content for admission here.", source="chat:low")
    store.add(leaf)
    await ExtractChunkJob().run(_ctx())
    await AppendBufferJob().run(_ctx())
    result = await SealJob().run(
        _ctx(max_buffer_leaves=20, max_buffer_age_seconds=999999)
    )
    assert result.payload["seals_written"] == 0
    refetched = store.get(leaf.id)
    assert refetched is not None
    assert refetched.state is LeafState.BUFFERED


async def test_seal_handles_missing_leaf_ids_gracefully(isolated_home: Path) -> None:
    """If a buffer carries an id that no longer resolves, the seal records
    the leaves that did resolve and the buffer is cleared regardless."""
    LeafBuffer("ghost:source").append("leaf_deadbeef")
    LeafBuffer("ghost:source").append("leaf_cafebabe")
    result = await SealJob().run(
        _ctx(max_buffer_leaves=2, max_buffer_age_seconds=999999)
    )
    assert result.payload["leaves_missing"] >= 2
    assert result.payload["seals_written"] == 0
    # Buffer was cleared so the next tick doesn't re-fire.
    assert LeafBuffer("ghost:source").read_ids() == []
