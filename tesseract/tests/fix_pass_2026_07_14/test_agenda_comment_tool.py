"""agenda_comment kernel tool — Option-B durability contract.

The tool is the ONLY writer of the ``role="agent"`` reply comment (mirrors
``workspace_reply``). Covers: happy-path write, unknown-item error, and
posture/risk-class declarations required by kernel-tool conventions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.kernel.tools.agenda_comment import AgendaCommentInput, AgendaCommentTool
from tesseract.kernel.tools.base import ToolContext
from tesseract.orchestrator.autonomy.agenda_comments import list_comments
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _seed_item(store: AgendaStore) -> AgendaItem:
    when = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    item = AgendaItem(
        id=mint_agenda_id("review", now=when),
        source=AgendaSource.MEMORY_SIGNAL,
        goal="Review discovery cluster",
        risk_class=RiskClass.PROPOSE,
        status=AgendaStatus.AWAITING_OPERATOR,
        created_at=when,
        updated_at=when,
    )
    store.add(item)
    return item


def _context() -> ToolContext:
    return ToolContext(session_id="test-session", current_call_id="call-1")


def test_declares_kernel_tool_conventions() -> None:
    """Posture/risk_class parity with `workspace_reply` (writes an
    operator-visible surface, same posture class)."""
    store = AgendaStore()
    tool = AgendaCommentTool(store=store)
    assert AgendaCommentTool.default_posture == "auto"
    assert AgendaCommentTool.risk_class == "autonomous"
    assert tool.is_read_only() is False
    assert tool.name == "agenda_comment"


@pytest.mark.asyncio
async def test_happy_path_writes_durable_agent_comment(isolated_home: Path) -> None:
    store = AgendaStore()
    item = _seed_item(store)
    tool = AgendaCommentTool(store=store)

    result = await tool.run(
        AgendaCommentInput(item_id=item.id, body="Approve — low risk."),
        _context(),
    )

    assert result.is_error is False
    assert result.metadata["item_id"] == item.id

    thread = list_comments(item.id)
    assert len(thread) == 1
    assert thread[0].role == "agent"
    assert thread[0].by == "tars"
    assert thread[0].body == "Approve — low risk."


@pytest.mark.asyncio
async def test_unknown_item_returns_error(isolated_home: Path) -> None:
    store = AgendaStore()
    tool = AgendaCommentTool(store=store)

    result = await tool.run(
        AgendaCommentInput(item_id="agd_does_not_exist", body="hello"),
        _context(),
    )

    assert result.is_error is True
    assert "not found" in result.output.lower()
    assert list_comments("agd_does_not_exist") == []


@pytest.mark.asyncio
async def test_empty_body_rejected(isolated_home: Path) -> None:
    store = AgendaStore()
    item = _seed_item(store)
    tool = AgendaCommentTool(store=store)

    result = await tool.run(
        AgendaCommentInput(item_id=item.id, body="   "),
        _context(),
    )

    assert result.is_error is True
    assert list_comments(item.id) == []
