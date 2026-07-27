"""Deferred 2026-07-12 — cross-process FAISS staleness.

A second process (controller CLI) holds its own EmbeddingIndex while the
Mirror process rewrites index.faiss on disk. Search paths now stat the
file (throttled) and swap in the on-disk index when it changed; a torn
mid-write file keeps the current index rather than degrading to empty.

Two EmbeddingIndex instances over the same derived_dir simulate the two
processes. Fakes only — no HTTP, no tesseract/logs writes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest


class _FakeEmbedder:
    @staticmethod
    def vector_for(text: str) -> list[float]:
        seed = sum(ord(c) for c in text)
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(8).astype(np.float32)
        v = v / max(np.linalg.norm(v), 1e-9)
        return v.tolist()


def _build(derived_dir: Path, monkeypatch):
    from tesseract.memory.embeddings import EmbeddingIndex

    idx = EmbeddingIndex(
        derived_dir=derived_dir,
        provider="fake",
        base_url="http://localhost",
        model="fake",
        dimensions=8,
        timeout_seconds=1.0,
        max_retries=1,
    )

    async def fake_embed_text(self, text: str):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        return _FakeEmbedder.vector_for(text)

    monkeypatch.setattr(EmbeddingIndex, "embed_text", fake_embed_text)
    return idx


@pytest.mark.asyncio
async def test_reader_sees_writer_process_changes(tmp_path: Path, monkeypatch):
    writer = _build(tmp_path, monkeypatch)
    await writer.add("mem-001", "the crawlspace flooding fix")

    # "Second process": constructed AFTER the first write, then goes stale.
    reader = _build(tmp_path, monkeypatch)
    assert reader._index.ntotal == 1  # noqa: SLF001

    await writer.add("mem-002", "gemma context window sizing")

    # Bypass the stat throttle; on Windows mtime granularity can make two
    # fast writes look identical — force the staleness signal.
    reader._next_reload_check = 0.0  # noqa: SLF001
    reader._loaded_mtime_ns = 0  # noqa: SLF001
    results = await reader.search("gemma context window sizing", top_k=2)

    assert reader._index.ntotal == 2  # noqa: SLF001
    assert any(mem_id == "mem-002" for mem_id, _ in results)


@pytest.mark.asyncio
async def test_torn_file_keeps_current_index(tmp_path: Path, monkeypatch):
    writer = _build(tmp_path, monkeypatch)
    await writer.add("mem-001", "durable entry")

    reader = _build(tmp_path, monkeypatch)
    assert reader._index.ntotal == 1  # noqa: SLF001

    # Simulate a mid-write torn index file from the other process.
    (tmp_path / "index.faiss").write_bytes(b"not a faiss file")
    reader._next_reload_check = 0.0  # noqa: SLF001
    reader._loaded_mtime_ns = 0  # noqa: SLF001

    results = await reader.search("durable entry", top_k=1)

    # Old in-memory index survives; search still answers.
    assert reader._index.ntotal == 1  # noqa: SLF001
    assert results and results[0][0] == "mem-001"


@pytest.mark.asyncio
async def test_own_save_does_not_trigger_reload(tmp_path: Path, monkeypatch):
    idx = _build(tmp_path, monkeypatch)
    await idx.add("mem-001", "self write")

    # After our own save, disk mtime equals the loaded marker — the next
    # search's throttled check must see "no change".
    idx._next_reload_check = 0.0  # noqa: SLF001
    assert idx._stat_mtime_ns() == idx._loaded_mtime_ns  # noqa: SLF001


@pytest.mark.asyncio
async def test_own_remove_does_not_trigger_reload(tmp_path: Path, monkeypatch):
    """Review finding 2026-07-13: remove() writes id_map.json without going
    through _save() — the marker must still refresh, or every memory_forget
    reads as a cross-process change on the next search."""
    idx = _build(tmp_path, monkeypatch)
    await idx.add("mem-001", "first")
    await idx.add("mem-002", "second")

    idx.remove("mem-001")

    assert idx._stat_mtime_ns() == idx._loaded_mtime_ns  # noqa: SLF001
