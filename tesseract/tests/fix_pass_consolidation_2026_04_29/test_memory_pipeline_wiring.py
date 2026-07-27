"""Audit M2 regression — `RetrievalPipeline` must be wired with
`fts_index` + `recall_log_path`, and must register `memory_search`
even when embeddings (Ollama) are unavailable.

Before 2026-04-29:
  - boot.py constructed `RetrievalPipeline(store, index, embeddings)`,
    silently dropping fts_index, recall_log_path, selector, path_expander,
    progress_cfg.
  - The pipeline only constructed when Ollama was up, so memory_search
    disappeared from the registry under degraded conditions.
  - `_log_recalls` was a no-op because recall_log_path was None.
  - `DreamingEngine` was never instantiated.
"""

from __future__ import annotations

from tesseract.brain.boot import build_memory_bundle
from tesseract.memory.dreaming import DreamingEngine


def test_pipeline_wired_with_fts_and_recall_log() -> None:
    bundle = build_memory_bundle()
    assert bundle.pipeline is not None, "pipeline must always construct (BM25 fallback)"
    pipeline = bundle.pipeline
    assert pipeline._fts_index is bundle.fts_index  # type: ignore[attr-defined]
    assert pipeline._recall_log_path == bundle.recall_log_path  # type: ignore[attr-defined]
    assert bundle.recall_log_path is not None
    assert bundle.recall_log_path.name == "recall.jsonl"


def test_dreaming_engine_instantiated() -> None:
    bundle = build_memory_bundle()
    assert isinstance(bundle.dreaming, DreamingEngine)
    assert bundle.dreaming._recall_log_path == bundle.recall_log_path  # type: ignore[attr-defined]


def test_pipeline_handles_embeddings_none(tmp_path) -> None:
    """RetrievalPipeline accepts embeddings=None without erroring at
    construction. Stage B then runs BM25-only."""
    from tesseract.memory.fts_index import FTSIndex
    from tesseract.memory.index import MemoryIndex
    from tesseract.memory.retrieval import RetrievalPipeline
    from tesseract.memory.store import MemoryStore

    derived = tmp_path / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(store_dir=tmp_path)
    index = MemoryIndex(store_dir=tmp_path)
    fts = FTSIndex(db_path=derived / "fts.db")
    pipeline = RetrievalPipeline(
        store=store,
        index=index,
        embeddings=None,
        fts_index=fts,
    )
    assert pipeline._embeddings is None  # type: ignore[attr-defined]
