"""M2 (audit-2 follow-up): memory_search threads include_work_history
through to RetrievalPipeline.retrieve and surfaces session/workshop
hits in a trust-labeled block alongside promoted memory.

Default is ON — the trust-text separation in the rendered output
makes the boundary explicit; the original CR-1 intent was that
per-turn recall surfaces work history automatically.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.memory_search import (
    MemorySearchInput,
    MemorySearchTool,
)
from tesseract.memory.retrieval import RetrievalPipeline
from tesseract.memory.work_index import WorkChunk, WorkIndex


class _FakeStore:
    def list_daily_notes(self) -> list[Path]:
        return []

    def list_all(self, type_filter=None):
        return []


class _FakeIndex:
    def load_raw(self) -> str:
        return ""


def _populated_pipeline(tmp_path: Path) -> RetrievalPipeline:
    wi = WorkIndex(tmp_path / "w.sqlite")
    wi.add(WorkChunk(
        source="session", source_path="2026-05-22.json", source_ref="sess-aud2",
        turn_idx=3, role="user", chunk_idx=0,
        ts="2026-05-22T10:00:00+00:00",
        text="audit follow-up discussion about retrieval merging",
    ))
    wi.add(WorkChunk(
        source="workshop",
        source_path="2026-05-22/retrieval-plan/README.md",
        source_ref="retrieval-plan",
        turn_idx=None, role=None, chunk_idx=0,
        ts="2026-05-22T08:00:00+00:00",
        text="retrieval merging plan with provenance labels",
    ))
    return RetrievalPipeline(
        store=_FakeStore(),  # type: ignore[arg-type]
        index=_FakeIndex(),  # type: ignore[arg-type]
        work_index=wi,
    )


@pytest.mark.asyncio
async def test_memory_search_default_surfaces_work_history(tmp_path: Path) -> None:
    pipeline = _populated_pipeline(tmp_path)
    tool = MemorySearchTool(pipeline=pipeline)
    result = await tool.run(
        MemorySearchInput(query="retrieval merging"),
        ToolContext(),
    )
    assert not result.is_error
    assert "WORK HISTORY" in result.output
    assert "non-authoritative" in result.output
    assert "session:sess-aud2" in result.output
    assert "workshop:retrieval-plan" in result.output


@pytest.mark.asyncio
async def test_memory_search_opt_out_suppresses_work_history(tmp_path: Path) -> None:
    pipeline = _populated_pipeline(tmp_path)
    tool = MemorySearchTool(pipeline=pipeline)
    result = await tool.run(
        MemorySearchInput(query="retrieval merging", include_work_history=False),
        ToolContext(),
    )
    # No promoted memory results AND no work history → empty-state message.
    assert "WORK HISTORY" not in result.output
    assert "No relevant memories" in result.output


@pytest.mark.asyncio
async def test_memory_search_work_history_only_still_returns_results(
    tmp_path: Path,
) -> None:
    """When promoted memory is empty but work-history has hits, the
    tool returns the work-history block — not the empty-state message
    (which would mislead the model into thinking there is nothing to
    say about a query that DOES have non-authoritative context)."""
    pipeline = _populated_pipeline(tmp_path)
    tool = MemorySearchTool(pipeline=pipeline)
    result = await tool.run(
        MemorySearchInput(query="provenance"),
        ToolContext(),
    )
    assert not result.is_error
    # No promoted-memory header because results=[] from _FakeStore.
    # But work-history hits exist for "provenance" via workshop chunk.
    assert "WORK HISTORY" in result.output
    assert "No relevant memories" not in result.output
