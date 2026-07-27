"""M3: RetrievalPipeline.retrieve(include_work_history=True) merges
work-history hits into the returned packet — under a distinct
non-authoritative provenance label, never folded into the promoted
memory results list.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tesseract.memory.retrieval import RetrievalPipeline
from tesseract.memory.types import RetrievalPacket
from tesseract.memory.work_index import WorkChunk, WorkIndex


class _FakeStore:
    def list_daily_notes(self) -> list[Path]:
        return []

    def list_all(self, type_filter=None):
        return []


class _FakeIndex:
    def load_raw(self) -> str:
        return ""

    def search_tags(self, *_a, **_k):
        return []


@pytest.mark.asyncio
async def test_retrieve_without_flag_returns_empty_work_history(
    tmp_path: Path,
) -> None:
    wi = WorkIndex(tmp_path / "w.sqlite")
    wi.add(WorkChunk(source="session", source_path="x.json", source_ref="s",
                     turn_idx=0, role="user", chunk_idx=0,
                     ts="2026-05-22", text="payload"))
    pipeline = RetrievalPipeline(
        store=_FakeStore(),  # type: ignore[arg-type]
        index=_FakeIndex(),  # type: ignore[arg-type]
        work_index=wi,
    )
    pkt = await pipeline.retrieve("payload")
    assert isinstance(pkt, RetrievalPacket)
    assert pkt.work_history == []


@pytest.mark.asyncio
async def test_retrieve_with_flag_includes_work_history(tmp_path: Path) -> None:
    wi = WorkIndex(tmp_path / "w.sqlite")
    wi.add(WorkChunk(source="session", source_path="x.json", source_ref="sess-1",
                     turn_idx=0, role="user", chunk_idx=0,
                     ts="2026-05-22", text="hermes comparison discussion"))
    wi.add(WorkChunk(source="workshop", source_path="y.md", source_ref="plan",
                     turn_idx=None, role=None, chunk_idx=0,
                     ts="2026-05-22", text="hermes comparison readme"))
    pipeline = RetrievalPipeline(
        store=_FakeStore(),  # type: ignore[arg-type]
        index=_FakeIndex(),  # type: ignore[arg-type]
        work_index=wi,
    )
    pkt = await pipeline.retrieve(
        "hermes comparison",
        include_work_history=True,
    )
    refs = {h.source_ref for h in pkt.work_history}
    assert refs == {"sess-1", "plan"}
    # work_history is NEVER folded into the memory results list.
    assert pkt.results == []


@pytest.mark.asyncio
async def test_retrieve_no_work_index_safely_returns_empty(tmp_path: Path) -> None:
    pipeline = RetrievalPipeline(
        store=_FakeStore(),  # type: ignore[arg-type]
        index=_FakeIndex(),  # type: ignore[arg-type]
        work_index=None,
    )
    pkt = await pipeline.retrieve(
        "anything",
        include_work_history=True,
    )
    assert pkt.work_history == []


def test_format_for_context_renders_work_history_block(tmp_path: Path) -> None:
    wi = WorkIndex(tmp_path / "w.sqlite")
    wi.add(WorkChunk(source="session", source_path="s.json", source_ref="sess-1",
                     turn_idx=2, role="user", chunk_idx=0,
                     ts="2026-05-22", text="autonomy discussion"))
    pipeline = RetrievalPipeline(
        store=_FakeStore(),  # type: ignore[arg-type]
        index=_FakeIndex(),  # type: ignore[arg-type]
        work_index=wi,
    )
    hits = wi.search("autonomy", top_k=3)
    packet = RetrievalPacket(results=[], work_history=hits)
    rendered = pipeline.format_for_context(packet)
    assert "WORK HISTORY" in rendered
    assert "non-authoritative" in rendered
    assert "session:sess-1" in rendered
    # And: the work-history hits are NOT under the "RETRIEVED MEMORIES"
    # header — they live in their own block.
    if "RETRIEVED MEMORIES" in rendered:
        mem_idx = rendered.find("RETRIEVED MEMORIES")
        wh_idx = rendered.find("WORK HISTORY")
        assert mem_idx < wh_idx, "work-history block must come AFTER memory"


def test_format_for_context_empty_when_nothing_to_render(tmp_path: Path) -> None:
    wi = WorkIndex(tmp_path / "w.sqlite")
    pipeline = RetrievalPipeline(
        store=_FakeStore(),  # type: ignore[arg-type]
        index=_FakeIndex(),  # type: ignore[arg-type]
        work_index=wi,
    )
    packet = RetrievalPacket(results=[], work_history=[])
    assert pipeline.format_for_context(packet) == ""
