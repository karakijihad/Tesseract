"""AU-16 S2 — three derived trees + memory_search tree-scoped extension."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.memory.leaf_seals import Seal, seals_root, write_seal
from tesseract.memory.leaves import (
    LeafState,
    LeafStore,
    MemoryLeaf,
    mint_leaf_id,
)
from tesseract.memory.tree_query import query as tree_query
from tesseract.memory.trees import (
    GLOBAL_TREES_ROOT,
    SOURCE_TREES_ROOT,
    TOPIC_ACTIVATION_THRESHOLD,
    activate_topic,
    daily_digest_path,
    is_topic_active,
    list_active_topics,
    read_daily_digest,
    read_source_tree,
    source_tree_path,
    write_daily_digest,
    write_seal_section,
)
from tesseract.scheduler.tasks.leaf_append import AppendBufferJob
from tesseract.scheduler.tasks.leaf_digest_daily import DigestDailyJob
from tesseract.scheduler.tasks.leaf_extract import ExtractChunkJob
from tesseract.scheduler.tasks.leaf_seal import SealJob
from tesseract.scheduler.tasks.leaf_topic_route import TopicRouteJob
from tesseract.scheduler.types import JobContext


def _ctx(**config) -> JobContext:
    return JobContext(job_name="test", config=dict(config))


def _make_leaf(
    body: str = "Plenty of body content here for the floor. " * 2,
    source: str = "chat:s1",
    entities: list[str] | None = None,
) -> MemoryLeaf:
    body = body
    if entities:
        body = body + " " + " ".join(f"[[{e}]]" for e in entities)
    now = datetime.now(timezone.utc)
    return MemoryLeaf(
        id=mint_leaf_id(),
        source=source,
        created_at=now,
        updated_at=now,
        body=body,
    )


def _make_seal(
    *,
    source_slug: str,
    leaf_count: int = 3,
    sealed_at: datetime | None = None,
) -> Seal:
    from tesseract.memory.leaf_seals import mint_seal_id

    when = sealed_at or datetime.now(timezone.utc)
    return Seal(
        seal_id=mint_seal_id(),
        source_slug=source_slug,
        sealed_at=when,
        leaf_ids=[f"leaf_{i:08x}" for i in range(leaf_count)],
        leaf_count=leaf_count,
        summary_title=f"Seal for {source_slug}",
        summary_body=f"# header\n\nbody for {source_slug}",
    )


# ---- source_tree ----------------------------------------------------


def test_write_seal_section_creates_file_with_banner(isolated_home: Path) -> None:
    seal = _make_seal(source_slug="chat-abc")
    write_seal(seal)
    write_seal_section(seal)
    body = read_source_tree("chat-abc")
    assert body is not None
    assert "# source: chat-abc" in body
    assert f"## Seal {seal.seal_id}" in body


def test_write_seal_section_newest_first_and_idempotent(isolated_home: Path) -> None:
    older = _make_seal(
        source_slug="chat-abc",
        sealed_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    newer = _make_seal(
        source_slug="chat-abc",
        sealed_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    write_seal_section(older)
    write_seal_section(newer)
    write_seal_section(newer)  # idempotent
    body = read_source_tree("chat-abc")
    assert body is not None
    older_idx = body.index(older.seal_id)
    newer_idx = body.index(newer.seal_id)
    assert newer_idx < older_idx
    assert body.count(newer.seal_id) == 1  # idempotent on retry


# ---- topic_tree -----------------------------------------------------


async def test_topic_route_activates_above_threshold(isolated_home: Path) -> None:
    store = LeafStore()
    # Three leaves each carrying entity [[ProjectX]] → activation should fire.
    leaves: list[MemoryLeaf] = []
    for _ in range(TOPIC_ACTIVATION_THRESHOLD):
        leaf = _make_leaf(entities=["ProjectX"])
        store.add(leaf)
        leaves.append(leaf)
    await ExtractChunkJob().run(_ctx())
    await AppendBufferJob().run(_ctx())
    await SealJob().run(_ctx(max_buffer_leaves=TOPIC_ACTIVATION_THRESHOLD))

    result = await TopicRouteJob().run(_ctx())
    assert result.ok
    assert "ProjectX" in result.payload["activated"]
    assert is_topic_active("ProjectX")


async def test_topic_route_does_not_activate_below_threshold(
    isolated_home: Path,
) -> None:
    store = LeafStore()
    # Only 2 occurrences — below default threshold of 3.
    for _ in range(2):
        store.add(_make_leaf(entities=["RareTopic"]))
    await ExtractChunkJob().run(_ctx())
    await AppendBufferJob().run(_ctx())
    await SealJob().run(_ctx(max_buffer_leaves=1, max_buffer_age_seconds=999999))

    result = await TopicRouteJob().run(_ctx())
    assert "RareTopic" not in result.payload["activated"]
    assert not is_topic_active("RareTopic")


async def test_topic_route_is_idempotent(isolated_home: Path) -> None:
    store = LeafStore()
    for _ in range(TOPIC_ACTIVATION_THRESHOLD):
        store.add(_make_leaf(entities=["Idempotent"]))
    await ExtractChunkJob().run(_ctx())
    await AppendBufferJob().run(_ctx())
    await SealJob().run(_ctx(max_buffer_leaves=TOPIC_ACTIVATION_THRESHOLD))

    first = await TopicRouteJob().run(_ctx())
    second = await TopicRouteJob().run(_ctx())
    # First run actually wrote something; second is a pure no-op (every
    # attempt is an idempotent skip).
    assert first.payload["sections_written"] >= 1
    assert second.payload["sections_written"] == 0
    assert second.payload["sections_skipped"] == first.payload["sections_written"]


# ---- global_tree ----------------------------------------------------


def test_global_digest_renders_seal_summary(isolated_home: Path) -> None:
    today = date(2026, 5, 19)
    when = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
    s1 = _make_seal(source_slug="src-a", leaf_count=3, sealed_at=when)
    s2 = _make_seal(
        source_slug="src-b",
        leaf_count=5,
        sealed_at=when + timedelta(hours=1),
    )
    path = write_daily_digest(today, [s1, s2])
    body = read_daily_digest(today)
    assert body is not None
    assert "2 seal" in body
    assert "8 leaves" in body
    assert "src-a" in body
    assert "src-b" in body
    # newest first
    assert body.index(s2.seal_id) < body.index(s1.seal_id)
    assert path == daily_digest_path(today)


async def test_digest_daily_job_writes_today(isolated_home: Path) -> None:
    when = datetime.now(timezone.utc)
    seal = _make_seal(source_slug="src-x", sealed_at=when)
    write_seal(seal)
    result = await DigestDailyJob().run(_ctx())
    assert result.ok
    target = read_daily_digest(when.date())
    assert target is not None
    assert seal.seal_id in target


# ---- memory_search tree-scope extension -----------------------------


async def test_memory_search_default_scope_unchanged(isolated_home: Path) -> None:
    """Existing callers (no scope kwarg) hit the legacy pipeline."""
    from tesseract.kernel.tools.memory_search import (
        MemorySearchInput,
        MemorySearchTool,
    )

    class _StubPipeline:
        async def retrieve(
            self,
            query,
            type_filter=None,
            top_k=5,
            *,
            include_work_history=False,
            work_history_top_k=5,
        ):
            from tesseract.memory.types import RetrievalPacket

            return RetrievalPacket(results=[], synthesis=None)

    tool = MemorySearchTool(_StubPipeline())
    result = await tool.run(
        MemorySearchInput(query="anything"),
        context=None,  # type: ignore[arg-type]
    )
    assert "No relevant memories" in result.output


async def test_memory_search_scope_source_returns_tree(isolated_home: Path) -> None:
    from tesseract.kernel.tools.memory_search import (
        MemorySearchInput,
        MemorySearchTool,
    )

    seal = _make_seal(source_slug="chat-show", sealed_at=datetime.now(timezone.utc))
    write_seal(seal)
    write_seal_section(seal)

    tool = MemorySearchTool(pipeline=None)  # type: ignore[arg-type]
    result = await tool.run(
        MemorySearchInput(query="", scope="source", source_slug="chat-show"),
        context=None,  # type: ignore[arg-type]
    )
    assert seal.seal_id in result.output
    assert "source/chat-show" in result.output


async def test_memory_search_scope_topic_returns_active_topic(
    isolated_home: Path,
) -> None:
    from tesseract.kernel.tools.memory_search import (
        MemorySearchInput,
        MemorySearchTool,
    )

    activate_topic("LiveTopic")
    tool = MemorySearchTool(pipeline=None)  # type: ignore[arg-type]
    result = await tool.run(
        MemorySearchInput(query="", scope="topic", entity="LiveTopic"),
        context=None,  # type: ignore[arg-type]
    )
    assert "topic/LiveTopic" in result.output


async def test_memory_search_invalid_scope_returns_error(isolated_home: Path) -> None:
    from tesseract.kernel.tools.memory_search import (
        MemorySearchInput,
        MemorySearchTool,
    )

    tool = MemorySearchTool(pipeline=None)  # type: ignore[arg-type]
    result = await tool.run(
        MemorySearchInput(query="", scope="bogus"),
        context=None,  # type: ignore[arg-type]
    )
    assert result.is_error
    assert "scope" in result.output.lower()


def test_tree_query_since_filters_older_sections(isolated_home: Path) -> None:
    older = _make_seal(
        source_slug="chat-since",
        sealed_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    newer = _make_seal(
        source_slug="chat-since",
        sealed_at=datetime(2026, 5, 19, tzinfo=timezone.utc),
    )
    write_seal_section(older)
    write_seal_section(newer)
    hits = tree_query(
        scope="source",
        source_slug="chat-since",
        since=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert len(hits) == 1
    assert newer.seal_id in hits[0].body
    assert older.seal_id not in hits[0].body
