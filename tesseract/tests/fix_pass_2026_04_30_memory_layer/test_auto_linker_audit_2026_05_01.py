"""Coverage for the four findings in `Docs/Audit/codex/2026-05-01/audit-1.md`.

M1 — incremental updates re-rank the union before clipping
M2 — semantic guardrails on top of cosine threshold
M3 — body block renders titles, frontmatter still bare IDs
M4 — auto_link returns AutoLinkResult; degraded runs surface to writes.jsonl

The tests use an in-memory `_FakeEmbeddings` instead of FAISS so they run
deterministically under sandboxed pytest without needing Ollama or a real
index. The fake honours the same surface AutoLinker actually consumes:
`get_vector`, `search_by_vector(candidate_ids=...)`, `search`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

from tesseract.memory.auto_linker import AutoLinker, AutoLinkResult
from tesseract.memory.related_block import (
    END_MARKER,
    START_MARKER,
    render_related_block,
    replace_related_block,
)
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType


def _write_memory(
    store_dir: Path,
    *,
    mid: str,
    mem_type: MemoryType = MemoryType.PROJECT,
    title: str | None = None,
    body: str = "Long enough body to clear the WhatNotToSave trivial-content threshold without effort, words.",
    auto_links: list[str] | None = None,
    entities: list[str] | None = None,
    tags: list[str] | None = None,
) -> None:
    folder = store_dir / mem_type.value
    folder.mkdir(parents=True, exist_ok=True)
    fm = MemoryFrontmatter(
        id=mid,
        type=mem_type,
        title=title or f"title-{mid}",
        created_at=datetime.now(timezone.utc),
        auto_links=auto_links or [],
        entities=entities or [],
        tags=tags or [],
    )
    text = (
        "---\n"
        + yaml.dump(fm.to_yaml_dict(), default_flow_style=False, sort_keys=False)
        + "---\n\n"
        + body
    )
    (folder / f"{mid}.md").write_text(text, encoding="utf-8")


class _FakeEmbeddings:
    """Stand-in for `EmbeddingIndex`. Score table drives every cosine answer.

    `scores[(src, nbr)] = float` is the cosine returned when `src` searches
    for `nbr`. Symmetric pairs must be entered explicitly — the audit's
    bidirectional concern is exercised by setting both directions.
    `vectors` controls whether `get_vector` returns a non-None value (the
    array contents don't matter; only `search_by_vector` is asked anything,
    and we route by source ID).
    """

    def __init__(
        self,
        scores: dict[tuple[str, str], float],
        vectors: dict[str, bool] | None = None,
    ) -> None:
        self._scores = scores
        self._vectors = vectors or {}
        self._last_search_source: str | None = None

    def get_vector(self, memory_id: str) -> np.ndarray | None:
        if self._vectors.get(memory_id, True):
            # Return a sentinel ndarray; AutoLinker only forwards it to
            # search_by_vector, which we override below to route by ID.
            return np.zeros(1, dtype=np.float32)
        return None

    def search_by_vector(
        self,
        vector: np.ndarray,
        top_k: int = 5,
        candidate_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        # AutoLinker calls get_vector(src) immediately before this. Re-derive
        # the source by scanning the score table: the most recent get_vector
        # was tracked.
        src = self._last_search_source or ""
        results: list[tuple[str, float]] = []
        for (s, n), score in self._scores.items():
            if s != src:
                continue
            if candidate_ids is not None and n not in candidate_ids:
                continue
            results.append((n, score))
        results.sort(key=lambda p: p[1], reverse=True)
        return results[:top_k]

    async def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        return []

    def set_source(self, src: str) -> None:
        # Tests call this before invoking AutoLinker so search_by_vector
        # knows which row of the score table to use.
        self._last_search_source = src


# --------------------------------------------------------------------- M3

def test_render_block_with_title_pairs_emits_alias_form() -> None:
    out = render_related_block([("mem_a", "Operator's note"), ("mem_b", "")])
    assert "[[mem_a|Operator's note]]" in out
    assert "[[mem_b]]" in out
    assert START_MARKER in out and END_MARKER in out


def test_render_block_legacy_bare_ids_still_work() -> None:
    out = render_related_block(["mem_a", "mem_b"])
    assert "[[mem_a]]" in out and "[[mem_b]]" in out
    assert "|" not in out  # no alias separator when no titles given


def test_replace_block_round_trips_with_title_pairs() -> None:
    body = "Body."
    once = replace_related_block(body, [("mem_a", "Alpha")])
    twice = replace_related_block(once, [("mem_a", "Alpha")])
    assert once == twice
    assert "[[mem_a|Alpha]]" in twice


# --------------------------------------------------------------------- M1

def test_rerank_displaces_older_link_when_new_is_more_similar(tmp_path: Path) -> None:
    """Five existing low-cosine links + one new high-cosine link → the new
    link displaces the lowest-scored existing one. Pre-fix this would have
    been impossible because the cap clipped from the tail of insertion order.
    """
    src = "mem_src"
    olds = [f"mem_old{i}" for i in range(5)]
    new = "mem_new"

    _write_memory(tmp_path, mid=src, auto_links=olds)
    for o in olds:
        _write_memory(tmp_path, mid=o)
    _write_memory(tmp_path, mid=new)

    # Source's vector ranks: new beats every old; olds get descending scores.
    score_table = {
        (src, new): 0.95,
        (src, olds[0]): 0.90,
        (src, olds[1]): 0.85,
        (src, olds[2]): 0.80,
        (src, olds[3]): 0.75,
        (src, olds[4]): 0.61,  # weakest existing link
    }
    fake = _FakeEmbeddings(score_table)
    store = MemoryStore(tmp_path)
    linker = AutoLinker(store=store, embeddings=fake)  # type: ignore[arg-type]

    fake.set_source(src)
    linker._add_auto_links(src, [new])

    fm_after, _ = store.read(src, log_access=False)
    assert new in fm_after.auto_links, "new high-cosine link must enter the cap"
    assert olds[4] not in fm_after.auto_links, "weakest old link must be displaced"
    assert len(fm_after.auto_links) == 5


def test_no_op_write_when_links_unchanged(tmp_path: Path) -> None:
    """Adding a link that's already present must NOT rewrite the file."""
    _write_memory(tmp_path, mid="mem_a", auto_links=["mem_b"])
    _write_memory(tmp_path, mid="mem_b")

    store = MemoryStore(tmp_path)
    linker = AutoLinker(store=store, embeddings=None)  # type: ignore[arg-type]

    path = next((tmp_path / "project").glob("mem_a.md"))
    mtime_before = path.stat().st_mtime_ns

    linker._add_auto_links("mem_a", ["mem_b"])

    mtime_after = path.stat().st_mtime_ns
    assert mtime_before == mtime_after, "file must not be rewritten when links don't change"


def test_rerank_falls_back_to_insertion_order_when_vector_missing(tmp_path: Path) -> None:
    """If the source has no embedded vector (e.g. Ollama was offline at save
    time), re-rank can't run — we must still preserve old behavior, not crash.
    """
    src = "mem_src"
    olds = [f"mem_old{i}" for i in range(5)]
    new = "mem_new"

    _write_memory(tmp_path, mid=src, auto_links=olds)
    for o in olds:
        _write_memory(tmp_path, mid=o)
    _write_memory(tmp_path, mid=new)

    fake = _FakeEmbeddings(scores={}, vectors={src: False})
    store = MemoryStore(tmp_path)
    linker = AutoLinker(store=store, embeddings=fake)  # type: ignore[arg-type]
    fake.set_source(src)

    linker._add_auto_links(src, [new])

    fm_after, _ = store.read(src, log_access=False)
    # Insertion-order fallback: olds keep their slots, new is dropped.
    assert fm_after.auto_links == olds


# --------------------------------------------------------------------- M2

def test_weak_cosine_without_overlap_is_rejected(tmp_path: Path) -> None:
    """Cosine in [0.6, 0.75) must require entity OR tag overlap."""
    src = "mem_src"
    nbr = "mem_nbr"
    _write_memory(tmp_path, mid=src, entities=["alpha"], tags=["x"])
    _write_memory(tmp_path, mid=nbr, entities=["beta"], tags=["y"])

    fake = _FakeEmbeddings({(src, nbr): 0.65})
    store = MemoryStore(tmp_path)
    linker = AutoLinker(store=store, embeddings=fake)  # type: ignore[arg-type]
    fake.set_source(src)

    result = asyncio.run(linker.auto_link(src, "body"))

    assert result.status == "skipped"
    assert result.reason == "no_neighbors"
    fm_after, _ = store.read(src, log_access=False)
    assert fm_after.auto_links == []


def test_weak_cosine_kept_when_entity_overlap(tmp_path: Path) -> None:
    """Same weak cosine, but with entity overlap — link is admitted."""
    src = "mem_src"
    nbr = "mem_nbr"
    _write_memory(tmp_path, mid=src, entities=["alpha"], tags=["x"])
    _write_memory(tmp_path, mid=nbr, entities=["alpha"], tags=["y"])

    fake = _FakeEmbeddings({(src, nbr): 0.65})
    store = MemoryStore(tmp_path)
    linker = AutoLinker(store=store, embeddings=fake)  # type: ignore[arg-type]
    fake.set_source(src)

    result = asyncio.run(linker.auto_link(src, "body"))

    assert result.status == "ok"
    assert nbr in result.linked


def test_strong_cosine_admitted_without_any_overlap(tmp_path: Path) -> None:
    """Cosine ≥ 0.75 admits regardless of entity/tag overlap — guardrails
    only kick in for weak pairs."""
    src = "mem_src"
    nbr = "mem_nbr"
    _write_memory(tmp_path, mid=src, entities=["alpha"], tags=["x"])
    _write_memory(tmp_path, mid=nbr, entities=["beta"], tags=["y"])

    fake = _FakeEmbeddings({(src, nbr): 0.80})
    store = MemoryStore(tmp_path)
    linker = AutoLinker(store=store, embeddings=fake)  # type: ignore[arg-type]
    fake.set_source(src)

    result = asyncio.run(linker.auto_link(src, "body"))

    assert result.status == "ok"
    assert nbr in result.linked


# --------------------------------------------------------------------- M3 + M4

def test_body_block_renders_neighbor_titles(tmp_path: Path) -> None:
    """After auto-linking, the body's `## Related` block must show neighbor
    titles in Obsidian alias form, not bare IDs."""
    src = "mem_src"
    nbr = "mem_nbr"
    _write_memory(tmp_path, mid=src, entities=["alpha"])
    _write_memory(tmp_path, mid=nbr, title="Neighbor Note", entities=["alpha"])

    fake = _FakeEmbeddings({(src, nbr): 0.85})
    store = MemoryStore(tmp_path)
    linker = AutoLinker(store=store, embeddings=fake)  # type: ignore[arg-type]
    fake.set_source(src)

    asyncio.run(linker.auto_link(src, "body"))

    fm_after, body_after = store.read(src, log_access=False)
    assert fm_after.auto_links == [nbr], "frontmatter still stores bare IDs"
    assert "[[mem_nbr|Neighbor Note]]" in body_after, "body shows alias form"


def test_auto_link_returns_skipped_when_embeddings_unavailable(tmp_path: Path) -> None:
    src = "mem_src"
    _write_memory(tmp_path, mid=src)

    store = MemoryStore(tmp_path)
    linker = AutoLinker(store=store, embeddings=None)  # type: ignore[arg-type]

    result = asyncio.run(linker.auto_link(src, "body"))

    assert isinstance(result, AutoLinkResult)
    assert result.status == "skipped"
    assert result.reason == "embeddings_unavailable"
    assert result.linked == []


def test_auto_link_returns_skipped_on_empty_index(tmp_path: Path) -> None:
    """Embeddings call returned []. Surface as `no_results` so memory_save can
    log the degraded run."""
    src = "mem_src"
    _write_memory(tmp_path, mid=src)

    fake = _FakeEmbeddings(scores={})  # nothing indexed for src
    store = MemoryStore(tmp_path)
    linker = AutoLinker(store=store, embeddings=fake)  # type: ignore[arg-type]
    fake.set_source(src)

    result = asyncio.run(linker.auto_link(src, "body"))

    assert result.status == "skipped"
    assert result.reason == "no_results"


def test_auto_link_returns_skipped_when_source_related_block_cannot_persist(tmp_path: Path) -> None:
    """M4 follow-up: if the source memory's derived `## Related` write-back
    returns False, auto_link must surface `persist_failed` instead of
    reporting success and leaving the failure in logs only.
    """
    src = "mem_src"
    nbr = "mem_nbr"
    _write_memory(tmp_path, mid=src, entities=["alpha"])
    _write_memory(tmp_path, mid=nbr, entities=["alpha"])

    fake = _FakeEmbeddings({(src, nbr): 0.85})

    class _FailingWriteStore(MemoryStore):
        def __init__(self, root: Path, fail_id: str) -> None:
            super().__init__(root)
            self._fail_id = fail_id

        def write(self, frontmatter, body, *args, **kwargs) -> bool:  # type: ignore[override]
            if frontmatter.id == self._fail_id and list(frontmatter.auto_links):
                return False
            return super().write(frontmatter, body, *args, **kwargs)

    store = _FailingWriteStore(tmp_path, fail_id=src)
    linker = AutoLinker(store=store, embeddings=fake)  # type: ignore[arg-type]
    fake.set_source(src)

    result = asyncio.run(linker.auto_link(src, "body"))

    assert result.status == "skipped"
    assert result.reason == "persist_failed"
    fm_after, body_after = store.read(src, log_access=False)
    assert fm_after.auto_links == []
    assert START_MARKER not in body_after


def test_memory_save_logs_skipped_auto_link(tmp_path: Path) -> None:
    """End-to-end M4: when auto_link returns skipped+degraded, memory_save
    writes a forensic event to events/writes.jsonl and tags the tool result."""
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.memory_save import MemorySaveInput, MemorySaveTool
    from tesseract.memory.index import MemoryIndex

    store = MemoryStore(tmp_path)
    index = MemoryIndex(store_dir=tmp_path)

    fake = _FakeEmbeddings(scores={})
    linker = AutoLinker(store=store, embeddings=fake)  # type: ignore[arg-type]

    class _DedupeBypass:
        async def add(self, *a, **k): return False
        def get_vector(self, *a, **k): return None
        def search_by_vector(self, *a, **k): return []
        async def search(self, *a, **k): return []

    tool = MemorySaveTool(
        store=store,
        index=index,
        embeddings=_DedupeBypass(),  # type: ignore[arg-type]
        auto_linker=linker,
    )

    inp = MemorySaveInput(
        type="project",
        title="Test save with degraded embeddings",
        content=(
            "Long enough body to clear the WhatNotToSave trivial-content "
            "minimum-character gate without ambiguity, so the save proceeds."
        ),
    )
    ctx = ToolContext(session_id="test-session")

    # Steer fake embeddings to "no_results" by leaving the score table empty
    # for whatever ID memory_save assigns. _add_auto_links won't be reached
    # because auto_link short-circuits on the empty result list.
    result = asyncio.run(tool.run(inp, ctx))

    assert not result.is_error
    assert "related-link generation skipped" in result.output

    writes_log = tmp_path / "events" / "writes.jsonl"
    assert writes_log.exists()
    lines = [json.loads(l) for l in writes_log.read_text(encoding="utf-8").splitlines() if l.strip()]
    skipped = [e for e in lines if e.get("type") == "auto_link_skipped"]
    assert skipped, "expected at least one auto_link_skipped event in writes.jsonl"
    assert skipped[-1]["reason"] in {"no_results", "embeddings_unavailable"}
