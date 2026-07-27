"""CR-1: ``recall_history`` tool — read-only retrieval with provenance.

Default posture AUTO (read-only). Returns formatted markdown with
``session:`` / ``workshop:`` labels and source paths so the model
can ``file_read`` for full context. NEVER promotes to memory.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.recall_history import (
    RecallHistoryInput,
    RecallHistoryTool,
)
from tesseract.memory.work_index import WorkChunk, WorkIndex


@pytest.fixture
def populated_index(tmp_path: Path) -> WorkIndex:
    idx = WorkIndex(tmp_path / "work.sqlite")
    idx.add(WorkChunk(
        source="session",
        source_path="tesseract/sessions/2026-05-21-2348.json",
        source_ref="sess-001",
        turn_idx=2,
        role="user",
        chunk_idx=0,
        ts="2026-05-21T23:48:00+00:00",
        text="hermes comparison and tars autonomy design discussion",
    ))
    idx.add(WorkChunk(
        source="workshop",
        source_path="tesseract/tars-workshop/2026-05-22/entity-autonomy-plan/README.md",
        source_ref="entity-autonomy-plan",
        turn_idx=None,
        role=None,
        chunk_idx=0,
        ts="2026-05-22T08:00:00+00:00",
        text="Entity autonomy plan — Phase 1 vertical slice with governed delegation.",
    ))
    return idx


def test_tool_default_posture_is_auto() -> None:
    assert RecallHistoryTool.default_posture == "auto"


def test_tool_is_read_only() -> None:
    tool = RecallHistoryTool(WorkIndex(":memory:"))
    assert tool.is_read_only() is True


@pytest.mark.asyncio
async def test_recall_returns_provenance_labels(populated_index: WorkIndex) -> None:
    tool = RecallHistoryTool(populated_index)
    result = await tool.run(
        RecallHistoryInput(query="autonomy"),
        ToolContext(),
    )
    assert not result.is_error
    body = result.output
    assert "session:" in body or "workshop:" in body
    # Both hits ranked together.
    assert "entity-autonomy-plan" in body or "sess-001" in body


@pytest.mark.asyncio
async def test_recall_filters_by_source(populated_index: WorkIndex) -> None:
    tool = RecallHistoryTool(populated_index)
    only_workshop = await tool.run(
        RecallHistoryInput(query="entity", source="workshop"),
        ToolContext(),
    )
    assert "workshop:" in only_workshop.output
    assert "session:" not in only_workshop.output


@pytest.mark.asyncio
async def test_recall_empty_query_returns_error(populated_index: WorkIndex) -> None:
    tool = RecallHistoryTool(populated_index)
    result = await tool.run(
        RecallHistoryInput(query=""),
        ToolContext(),
    )
    assert result.is_error


@pytest.mark.asyncio
async def test_recall_empty_result_set(populated_index: WorkIndex) -> None:
    tool = RecallHistoryTool(populated_index)
    result = await tool.run(
        RecallHistoryInput(query="zzqquuv-no-such-token"),
        ToolContext(),
    )
    assert not result.is_error
    assert "no results" in result.output.lower() or "no matches" in result.output.lower()


@pytest.mark.asyncio
async def test_recall_output_carries_trust_text(populated_index: WorkIndex) -> None:
    """The output must remind the caller that work-history hits are
    non-authoritative — they are suggestions, not promoted facts."""
    tool = RecallHistoryTool(populated_index)
    result = await tool.run(
        RecallHistoryInput(query="autonomy"),
        ToolContext(),
    )
    lower = result.output.lower()
    assert "non-authoritative" in lower or "not promoted" in lower or "suggestion" in lower
