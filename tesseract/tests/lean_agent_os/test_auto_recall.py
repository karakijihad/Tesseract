"""lean-agent-os P1 Task 3 — auto memory retrieval per turn.

Covers:
  1. Relevant memories surface in the injected `[recalled_memories]` block.
  2. Below-floor hits are dropped — no block at all.
  3. A retriever failure (embedder down) degrades to no block; the caller
     is unaffected (best-effort per CLAUDE.md memory rule).
  4. `char_cap` truncates a long memory body to one line.
  5. Missing `memory.yaml::auto_recall` keys raise loudly at load.
  6. End-to-end via `ChatSession.send`: the block reaches the adapter
     payload before the model call and is never persisted to history.
"""

from __future__ import annotations

from typing import AsyncGenerator

import pytest

from tesseract.brain import auto_recall as auto_recall_module
from tesseract.brain.auto_recall import (
    RecallItem,
    auto_recall,
    format_recall_block,
    load_auto_recall_config,
)
from pydantic import BaseModel

from tesseract.brain.chat import ChatSession
from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter, StreamChunk
from tesseract.memory.retrieval import MAX_FINAL_RESULTS, RetrievalResult
from tesseract.memory.types import MemoryType, RetrievalPacket


class _EmptyInput(BaseModel):
    pass


class _FakeRetriever:
    """Stand-in for `RetrievalPipeline` — returns a canned packet or raises.

    Respects `top_k` the way the real `RetrievalPipeline.retrieve` does:
    returns at most `top_k` results, drawn from `results` (a candidate pool
    assumed already sorted in relevance order) — NEVER the full pool
    regardless of what `auto_recall` asked for. This is the seam Finding 1
    covers: a fake that ignores `top_k` can't exercise the real bounded
    backfill behavior.
    """

    def __init__(self, results: list[RetrievalResult] | None = None, raise_exc: Exception | None = None) -> None:
        self._results = results or []
        self._raise_exc = raise_exc
        self.calls: list[str] = []
        self.requested_top_ks: list[int] = []

    async def retrieve(self, query: str, *, top_k: int, include_work_history: bool) -> RetrievalPacket:
        self.calls.append(query)
        self.requested_top_ks.append(top_k)
        if self._raise_exc is not None:
            raise self._raise_exc
        return RetrievalPacket(results=list(self._results[:top_k]))


def _result(memory_id: str, title: str, body: str, score: float) -> RetrievalResult:
    return RetrievalResult(
        memory_id=memory_id, title=title, body=body, score=score, mem_type=MemoryType.PROJECT,
    )


# -- auto_recall() unit coverage -----------------------------------------


async def test_relevant_memories_surface_in_block() -> None:
    retriever = _FakeRetriever(results=[
        _result("mem_1", "Voice default", "Conversational mode by default.", score=0.9),
        _result("mem_2", "Orb colors", "Cyan idle, amber busy.", score=0.5),
    ])

    items = await auto_recall(
        "what did we decide about the orb?", retriever,
        top_k=5, char_cap=300, min_similarity=0.1, min_query_words=1,
    )

    assert [it.memory_id for it in items] == ["mem_1", "mem_2"]
    block = format_recall_block(items)
    assert block.startswith("[recalled_memories]")
    assert block.endswith("[/recalled_memories]")
    assert "mem_1" in block and "0.90" in block
    assert "mem_2" in block and "0.50" in block


async def test_below_floor_returns_nothing() -> None:
    retriever = _FakeRetriever(results=[
        _result("mem_low", "Stale fact", "Barely relevant.", score=0.01),
    ])

    items = await auto_recall(
        "some query", retriever, top_k=5, char_cap=300, min_similarity=0.5, min_query_words=1,
    )

    assert items == []
    assert format_recall_block(items) == ""


async def test_embedder_raising_degrades_to_no_block() -> None:
    retriever = _FakeRetriever(raise_exc=RuntimeError("ollama down"))

    items = await auto_recall(
        "some query", retriever, top_k=5, char_cap=300, min_similarity=0.1, min_query_words=1,
    )

    assert items == []
    assert format_recall_block(items) == ""


async def test_char_cap_truncates_long_body() -> None:
    long_body = "x" * 500
    retriever = _FakeRetriever(results=[_result("mem_long", "Long memory", long_body, score=0.9)])

    items = await auto_recall(
        "query", retriever, top_k=5, char_cap=50, min_similarity=0.1, min_query_words=1,
    )

    assert len(items) == 1
    assert len(items[0].text) <= 50
    assert items[0].text.endswith("…")


async def test_empty_query_short_circuits() -> None:
    retriever = _FakeRetriever(results=[_result("mem_1", "T", "B", score=0.9)])

    items = await auto_recall(
        "   ", retriever, top_k=5, char_cap=300, min_similarity=0.1, min_query_words=1,
    )

    assert items == []
    assert retriever.calls == []


# -- min_query_words trivial-message guard (review fix #2) ----------------


async def test_trivial_short_message_short_circuits_before_retrieval() -> None:
    retriever = _FakeRetriever(results=[_result("mem_1", "T", "B", score=0.9)])

    items = await auto_recall(
        "ok", retriever, top_k=5, char_cap=300, min_similarity=0.01, min_query_words=3,
    )

    assert items == []
    assert retriever.calls == []


async def test_query_meeting_min_words_reaches_retriever() -> None:
    retriever = _FakeRetriever(results=[_result("mem_1", "T", "B", score=0.9)])

    items = await auto_recall(
        "three word query", retriever, top_k=5, char_cap=300, min_similarity=0.01, min_query_words=3,
    )

    assert retriever.calls == ["three word query"]
    assert [it.memory_id for it in items] == ["mem_1"]


# -- min_similarity floor boundary at 0.03 (review fix #1) -----------------


async def test_floor_excludes_single_route_rank0_score() -> None:
    """A single-route RRF rank-0 hit (1/(60+0) ~= 0.0167) sits below 0.03."""
    retriever = _FakeRetriever(results=[_result("mem_1", "T", "B", score=1 / 60)])

    items = await auto_recall(
        "query about something specific", retriever,
        top_k=5, char_cap=300, min_similarity=0.03, min_query_words=1,
    )

    assert items == []


async def test_floor_admits_multi_route_consensus_score() -> None:
    """Two routes agreeing at rank-0 (2/(60+0) ~= 0.0333) clears 0.03."""
    retriever = _FakeRetriever(results=[_result("mem_1", "T", "B", score=2 / 60)])

    items = await auto_recall(
        "query about something specific", retriever,
        top_k=5, char_cap=300, min_similarity=0.03, min_query_words=1,
    )

    assert [it.memory_id for it in items] == ["mem_1"]


# -- real config floor recalibration (RC3, memory-retrieval-diagnosis.md,
#    2026-07-08) --------------------------------------------------------


async def test_real_config_floor_admits_single_route_rank0_hit() -> None:
    """The real memory.yaml floor must sit below the single-route RRF
    rank-0 ceiling (1/60 ~= 0.0167) so a genuine vector-only or BM25-only
    hit clears it — the pre-recalibration 0.03 floor made this
    unreachable whenever only one route matched (e.g. FTS down, RC2)."""
    cfg = load_auto_recall_config()
    assert cfg.min_similarity < (1 / 60)
    retriever = _FakeRetriever(results=[_result("mem_1", "T", "B", score=1 / 60)])

    items = await auto_recall(
        "query about something specific", retriever,
        top_k=5, char_cap=300, min_similarity=cfg.min_similarity, min_query_words=1,
    )

    assert [it.memory_id for it in items] == ["mem_1"]


async def test_real_config_floor_still_drops_garbage_score() -> None:
    """A near-zero score (well below any real single-route RRF rank) must
    still be dropped — recalibration must not disable filtering entirely."""
    cfg = load_auto_recall_config()
    garbage_score = cfg.min_similarity / 2
    retriever = _FakeRetriever(results=[_result("mem_low", "T", "B", score=garbage_score)])

    items = await auto_recall(
        "query about something specific", retriever,
        top_k=5, char_cap=300, min_similarity=cfg.min_similarity, min_query_words=1,
    )

    assert items == []


# -- exclude_ids cross-turn dedup, pure-function level (review fix #3) -----


async def test_exclude_ids_skips_and_backfills_from_next_candidate() -> None:
    """A deduped id is skipped BEFORE the top_k cap: the next candidate
    takes its slot rather than the block shrinking by one."""
    retriever = _FakeRetriever(results=[
        _result("mem_1", "T1", "B1", score=0.9),
        _result("mem_2", "T2", "B2", score=0.8),
    ])

    items = await auto_recall(
        "some relevant query", retriever,
        top_k=1, char_cap=300, min_similarity=0.01, min_query_words=1,
        exclude_ids={"mem_1"},
    )

    assert [it.memory_id for it in items] == ["mem_2"]


def _pool(n: int) -> list[RetrievalResult]:
    """`n` results, descending score, all clearing a low similarity floor."""
    return [_result(f"mem_{i}", f"T{i}", f"B{i}", score=0.90 - i * 0.05) for i in range(n)]


async def test_auto_recall_overfetches_to_max_final_results_for_backfill_room() -> None:
    """`auto_recall` must request `MAX_FINAL_RESULTS` candidates from the
    retriever, not the caller's `top_k` — `RetrievalPipeline.retrieve` caps
    ITS OWN output at `MAX_FINAL_RESULTS` regardless of `top_k`, so asking
    for exactly `top_k` leaves no surplus for dedup to backfill from
    (Finding 1)."""
    retriever = _FakeRetriever(results=_pool(MAX_FINAL_RESULTS))

    await auto_recall(
        "some relevant query", retriever,
        top_k=5, char_cap=300, min_similarity=0.01, min_query_words=1,
    )

    assert retriever.requested_top_ks == [MAX_FINAL_RESULTS]


async def test_exclude_ids_within_surplus_backfills_to_full_top_k() -> None:
    """Excluded-id count <= the retriever's surplus above top_k
    (`MAX_FINAL_RESULTS - top_k`): the block backfills to the full
    configured `top_k` from fresh candidates."""
    top_k = 5
    surplus = MAX_FINAL_RESULTS - top_k
    retriever = _FakeRetriever(results=_pool(MAX_FINAL_RESULTS))
    excluded = {f"mem_{i}" for i in range(surplus)}  # exactly at the surplus

    items = await auto_recall(
        "some relevant query", retriever,
        top_k=top_k, char_cap=300, min_similarity=0.01, min_query_words=1,
        exclude_ids=excluded,
    )

    assert len(items) == top_k
    assert not (excluded & {it.memory_id for it in items})


async def test_exclude_ids_beyond_surplus_shrinks_block_gracefully() -> None:
    """Excluded-id count > the retriever's surplus above top_k: the block
    shrinks below `top_k` instead of crashing, duplicating, or re-injecting
    an excluded id — the real (bounded) invariant, not the myth that
    backfill always restores the full block."""
    top_k = 5
    surplus = MAX_FINAL_RESULTS - top_k
    retriever = _FakeRetriever(results=_pool(MAX_FINAL_RESULTS))
    excluded = {f"mem_{i}" for i in range(surplus + 2)}  # 2 more than the surplus can cover

    items = await auto_recall(
        "some relevant query", retriever,
        top_k=top_k, char_cap=300, min_similarity=0.01, min_query_words=1,
        exclude_ids=excluded,
    )

    expected_remaining = MAX_FINAL_RESULTS - len(excluded)
    assert len(items) == expected_remaining
    assert len(items) < top_k
    ids = [it.memory_id for it in items]
    assert len(ids) == len(set(ids))  # no duplicates
    assert not (excluded & set(ids))  # no re-injection of excluded ids


# -- config loading --------------------------------------------------------


def test_load_auto_recall_config_reads_real_yaml() -> None:
    cfg = load_auto_recall_config()

    assert cfg.top_k > 0
    assert cfg.char_cap > 0
    assert cfg.min_similarity >= 0.0


def test_missing_config_key_raises_loudly(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad_yaml = tmp_path / "memory.yaml"
    bad_yaml.write_text("auto_recall:\n  top_k: 5\n  char_cap: 300\n", encoding="utf-8")
    monkeypatch.setattr(auto_recall_module, "MEMORY_YAML", bad_yaml)

    with pytest.raises(RuntimeError, match="min_similarity"):
        load_auto_recall_config()


def test_missing_auto_recall_section_raises_loudly(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad_yaml = tmp_path / "memory.yaml"
    bad_yaml.write_text("other: {}\n", encoding="utf-8")
    monkeypatch.setattr(auto_recall_module, "MEMORY_YAML", bad_yaml)

    with pytest.raises(RuntimeError, match="auto_recall"):
        load_auto_recall_config()


# -- ChatSession.send() integration ----------------------------------------


class _CaptureAdapter(ModelAdapter):
    def __init__(self) -> None:
        self.seen_messages: list[dict] = []

    async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
        return "unused"

    async def stream(self, messages, tools=None, options=None) -> AsyncGenerator[StreamChunk, None]:
        self.seen_messages = list(messages)
        yield StreamChunk(type=ChunkType.TEXT, text="hello back")
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")

    def count_tokens(self, messages) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


class _StubMemoryTool:
    """Duck-typed stand-in for `MemorySearchTool`. chat.py reads `.pipeline`;
    `ToolRegistry.schemas_for_adapter` also needs `.name` + `.tier` on every
    registered tool, so this fixture carries the same minimal surface."""

    name = "memory_search"
    tier = "core"
    description = "stub memory_search"
    input_schema = _EmptyInput

    def __init__(self, pipeline: _FakeRetriever) -> None:
        self.pipeline = pipeline


def _session_with_memory_tool(adapter: _CaptureAdapter, pipeline: _FakeRetriever) -> ChatSession:
    registry = ToolRegistry(tools={"memory_search": _StubMemoryTool(pipeline)})
    return ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
        registry=registry,
    )


async def test_send_injects_recall_block_before_model_call() -> None:
    adapter = _CaptureAdapter()
    pipeline = _FakeRetriever(results=[_result("mem_1", "Orb colors", "Cyan idle.", score=0.9)])
    session = _session_with_memory_tool(adapter, pipeline)

    async for _chunk in session.send("what did we decide about the orb colors?"):
        pass

    injected = [
        m for m in adapter.seen_messages
        if m.get("role") == "user" and "[recalled_memories]" in str(m.get("content"))
    ]
    assert len(injected) == 1
    assert "mem_1" in injected[0]["content"]
    # never persisted to history — only the real user/assistant turns are.
    assert all("[recalled_memories]" not in str(m.get("content")) for m in session.history)


async def test_send_proceeds_when_retriever_raises() -> None:
    adapter = _CaptureAdapter()
    pipeline = _FakeRetriever(raise_exc=RuntimeError("ollama down"))
    session = _session_with_memory_tool(adapter, pipeline)

    async for _chunk in session.send("weeks-old topic with no recall keyword"):
        pass

    assert not any("[recalled_memories]" in str(m.get("content")) for m in adapter.seen_messages)
    assert session.history[-1]["role"] == "assistant"


def _recall_block_from_messages(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "user" and "[recalled_memories]" in str(m.get("content")):
            return str(m.get("content"))
    return ""


async def test_send_no_recall_block_when_all_hits_below_floor() -> None:
    """review fix #4 — send()-level below-floor/empty path. RC3
    recalibration (2026-07-08): the real `memory.yaml` floor now admits a
    single-route RRF rank-0 score (~0.0167), so this exercises a score
    genuinely below the new floor (0.01) instead."""
    adapter = _CaptureAdapter()
    pipeline = _FakeRetriever(results=[_result("mem_low", "Stale fact", "Barely relevant.", score=0.001)])
    session = _session_with_memory_tool(adapter, pipeline)

    async for _chunk in session.send("what did we discuss about that topic last week?"):
        pass

    assert not any("[recalled_memories]" in str(m.get("content")) for m in adapter.seen_messages)


async def test_dedup_window_excludes_then_readmits_after_window() -> None:
    """review fix #3, ChatSession level — a memory id injected on turn 1
    is excluded from re-injection for `dedup_window_turns` (3, per the
    real `memory.yaml`) subsequent turns, then re-admitted."""
    adapter = _CaptureAdapter()
    pipeline = _FakeRetriever(results=[_result("mem_1", "T1", "B1", score=0.9)])
    session = _session_with_memory_tool(adapter, pipeline)

    async for _chunk in session.send("first relevant query about the topic"):
        pass
    assert "mem_1" in _recall_block_from_messages(adapter.seen_messages)

    for _ in range(3):
        async for _chunk in session.send("another relevant query about the topic"):
            pass
        assert _recall_block_from_messages(adapter.seen_messages) == ""

    async for _chunk in session.send("final relevant query about the topic"):
        pass
    assert "mem_1" in _recall_block_from_messages(adapter.seen_messages)
