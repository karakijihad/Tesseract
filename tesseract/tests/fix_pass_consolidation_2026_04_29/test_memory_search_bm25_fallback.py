"""Audit M2 regression — `memory_search` must return results from BM25
when embeddings are unavailable.

Before 2026-04-29 the pipeline required embeddings to construct, so
memory_search vanished from the registry whenever Ollama was down. The
audit's specific complaint: "memory_search should still work through
FTS when vector embeddings fail."

This test seeds the store + FTS index with a memory and queries via
the pipeline with embeddings=None.
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


def _seed(tmp_path: Path) -> tuple[MemoryStore, FTSIndex, MemoryIndex]:
    store_dir = tmp_path / "memory-store"
    derived_dir = store_dir / "derived"
    derived_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(store_dir=store_dir)
    index = MemoryIndex(store_dir=store_dir)
    fts = FTSIndex(db_path=derived_dir / "fts.db")
    now = datetime.now(timezone.utc)
    fm = MemoryFrontmatter(
        id="mem_test-bm25-recall",
        type=MemoryType.PROJECT,
        title="Tesseract permission engine",
        summary="The permission engine evaluates tool calls.",
        tags=["permissions", "tesseract"],
        entities=[],
        importance=8,
        created_at=now,
        updated_at=now,
    )
    body = (
        "The permission engine evaluates each tool call through a layered "
        "pipeline: deny rules first, then ask rules, then path validation, "
        "then mode baseline. Returns a final decision the executor honors."
    )
    assert store.write(fm, body) is True, "store.write must not be blocked by WhatNotToSave"
    fts.rebuild([(fm.id, fm.title, body)])
    index.add(fm)
    return store, fts, index


def test_bm25_returns_results_when_embeddings_none(tmp_path: Path) -> None:
    store, fts, index = _seed(tmp_path)
    pipeline = RetrievalPipeline(
        store=store,
        index=index,
        embeddings=None,
        fts_index=fts,
    )
    packet = asyncio.run(pipeline.retrieve("permission engine"))
    assert packet.results, "BM25-only retrieval should return at least one hit"
    assert any(r.memory_id == "mem_test-bm25-recall" for r in packet.results)


def test_recall_log_written_when_path_provided(tmp_path: Path) -> None:
    store, fts, index = _seed(tmp_path)
    recall_log = tmp_path / "derived" / "recall.jsonl"
    pipeline = RetrievalPipeline(
        store=store,
        index=index,
        embeddings=None,
        fts_index=fts,
        recall_log_path=recall_log,
    )
    asyncio.run(pipeline.retrieve("permission engine"))
    assert recall_log.exists(), "recall.jsonl must be written when results return"
    lines = recall_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    import json

    entry = json.loads(lines[0])
    assert entry["memory_id"] == "mem_test-bm25-recall"
    assert entry["query"] == "permission engine"


def test_recall_log_skipped_when_path_none(tmp_path: Path) -> None:
    store, fts, index = _seed(tmp_path)
    pipeline = RetrievalPipeline(
        store=store,
        index=index,
        embeddings=None,
        fts_index=fts,
        recall_log_path=None,
    )
    asyncio.run(pipeline.retrieve("permission engine"))
    # No recall.jsonl path was set, so nothing should be written.
    candidate = tmp_path / "derived" / "recall.jsonl"
    assert not candidate.exists()
