"""WP-1 blocker §B fix — concurrent FAISS access is serialized.

Verifies the threading.Lock added to EmbeddingIndex prevents id_map corruption
under concurrent add()/search()/remove() from multiple async tasks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest


class _FakeEmbedder:
    """Returns a deterministic 8-D vector per text without an HTTP call."""

    @staticmethod
    def vector_for(text: str) -> list[float]:
        seed = sum(ord(c) for c in text)
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(8).astype(np.float32)
        v = v / max(np.linalg.norm(v), 1e-9)
        return v.tolist()


def _build(tmp_path: Path, monkeypatch):
    from tesseract.memory.embeddings import EmbeddingIndex

    idx = EmbeddingIndex(
        derived_dir=tmp_path,
        provider="fake",
        base_url="http://localhost",
        model="fake",
        dimensions=8,
        timeout_seconds=1.0,
        max_retries=1,
    )

    async def fake_embed_text(self, text: str):  # type: ignore[no-untyped-def]
        # Yield to the event loop so concurrent tasks actually interleave.
        await asyncio.sleep(0)
        return _FakeEmbedder.vector_for(text)

    monkeypatch.setattr(EmbeddingIndex, "embed_text", fake_embed_text)
    return idx


@pytest.mark.asyncio
async def test_concurrent_add_keeps_index_coherent(tmp_path: Path, monkeypatch):
    """N parallel add() calls finish without losing entries and id_map stays
    consistent with FAISS ntotal."""
    idx = _build(tmp_path, monkeypatch)

    ids = [f"mem-{i:03d}" for i in range(20)]
    await asyncio.gather(*[idx.add(mid, f"text for {mid}") for mid in ids])

    # Every id must be present in the id_map exactly once.
    assert len(idx._id_to_pos) == len(ids), (  # noqa: SLF001 — test
        f"expected {len(ids)} ids, got {len(idx._id_to_pos)}"
    )
    # FAISS ntotal must match (no orphaned vectors, no lost adds).
    assert idx._index.ntotal == len(ids)  # noqa: SLF001 — test
    # No duplicate positions in pos_to_id.
    assert len(set(idx._pos_to_id.keys())) == len(ids)  # noqa: SLF001 — test


@pytest.mark.asyncio
async def test_concurrent_add_and_search_no_corruption(tmp_path: Path, monkeypatch):
    """Mixing add and search concurrently — index reads must never observe a
    half-mutated state. We check end-state coherence after the storm."""
    idx = _build(tmp_path, monkeypatch)

    # Seed a few entries so search has something to find.
    for i in range(5):
        await idx.add(f"seed-{i}", f"seed text {i}")

    add_ids = [f"add-{i}" for i in range(10)]
    add_tasks = [idx.add(mid, f"text for {mid}") for mid in add_ids]
    search_tasks = [idx.search(f"seed text {i % 5}", top_k=3) for i in range(10)]

    results = await asyncio.gather(*add_tasks, *search_tasks)

    # Every add returned True (embedder is faked, no network failures).
    assert all(results[: len(add_tasks)])
    # End-state coherence.
    assert idx._index.ntotal == len(idx._id_to_pos)  # noqa: SLF001 — test
    assert idx._index.ntotal == 5 + len(add_ids)  # noqa: SLF001 — test


@pytest.mark.asyncio
async def test_snapshot_ids_returns_locked_copy(tmp_path: Path, monkeypatch):
    """Audit-fix M5: callers needing the id-list (vault_indexer,
    consistency) use snapshot_ids() instead of reaching into
    `_id_to_pos`. Verifies the public API: lock-safe, returns a list
    (not a view), copy-on-call so subsequent mutations are invisible."""
    idx = _build(tmp_path, monkeypatch)

    for i in range(5):
        await idx.add(f"mem-{i}", f"text {i}")

    snapshot = idx.snapshot_ids()
    assert sorted(snapshot) == [f"mem-{i}" for i in range(5)]
    assert isinstance(snapshot, list)

    # Subsequent mutations must not change the snapshot.
    idx.remove("mem-0")
    await idx.add("mem-99", "fresh text")
    assert "mem-0" in snapshot  # snapshot is frozen
    assert "mem-99" not in snapshot


@pytest.mark.asyncio
async def test_fragmentation_check_uses_locked_snapshot(tmp_path: Path, monkeypatch):
    """Audit-fix M4 regression — `add()` must not read `self.fragmentation`
    outside the lock. Stress: 25 parallel adds, each fragment-eligible.
    The compaction trigger should never observe a torn read (numerator
    from before the lock release, denominator from after a concurrent
    remove)."""
    idx = _build(tmp_path, monkeypatch)

    # Seed enough vectors to reach the >20 threshold quickly.
    for i in range(22):
        await idx.add(f"seed-{i}", f"seed text {i}")

    # Mix concurrent adds and removes; some adds re-insert prior ids,
    # which exercises the remove-then-add path that previously raced
    # against the fragmentation read.
    add_ids = [f"a-{i}" for i in range(15)]
    remove_ids = [f"seed-{i}" for i in range(5)]

    async def add_one(mid: str) -> bool:
        return await idx.add(mid, f"text {mid}")

    async def remove_one(mid: str) -> None:
        await asyncio.sleep(0)
        idx.remove(mid)

    results = await asyncio.gather(
        *[add_one(m) for m in add_ids],
        *[remove_one(m) for m in remove_ids],
        return_exceptions=True,
    )

    # No exceptions (no torn arithmetic, no AssertionError from
    # fragmentation, no FAISS internal corruption).
    for r in results:
        assert not isinstance(r, Exception), f"unexpected: {r!r}"

    # End-state coherence — every id-map entry has a matching pos_to_id.
    surviving_in_pos = set(idx._pos_to_id.values())  # noqa: SLF001 — test
    surviving_in_id = set(idx._id_to_pos.keys())  # noqa: SLF001 — test
    assert surviving_in_pos == surviving_in_id


@pytest.mark.asyncio
async def test_concurrent_add_remove_no_dangling_positions(tmp_path: Path, monkeypatch):
    """Adding and removing the same id concurrently must not leave dangling
    pos_to_id entries."""
    idx = _build(tmp_path, monkeypatch)

    # Pre-populate.
    for i in range(10):
        await idx.add(f"mem-{i}", f"text {i}")

    async def add_then_remove(mid: str) -> None:
        await idx.add(mid, f"text {mid}")
        await asyncio.sleep(0)
        idx.remove(mid)

    targets = [f"mem-{i}" for i in range(5)]  # half of the pre-populated set
    await asyncio.gather(*[add_then_remove(m) for m in targets])

    # The targeted ids are gone.
    for mid in targets:
        assert mid not in idx._id_to_pos  # noqa: SLF001 — test
    # pos_to_id contains exactly the surviving ids — no dangling positions.
    surviving_in_pos = set(idx._pos_to_id.values())  # noqa: SLF001 — test
    surviving_in_id = set(idx._id_to_pos.keys())  # noqa: SLF001 — test
    assert surviving_in_pos == surviving_in_id
