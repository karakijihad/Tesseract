"""``rationale.py`` — bounded model wrapper with timeout + failure
fallback."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from tesseract.orchestrator.autonomy import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    UNAVAILABLE_MARKER,
    generate_rationale,
    mint_agenda_id,
)
from tesseract.orchestrator.autonomy.rationale import build_prompt


def _make_item(goal: str = "doe-rationale", priority: float = 12.0) -> AgendaItem:
    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    return AgendaItem(
        id=mint_agenda_id(goal, now=now),
        created_at=now,
        updated_at=now,
        source=AgendaSource.OPERATOR,
        goal=goal,
        risk_class=RiskClass.PROPOSE,
        status=AgendaStatus.PROPOSED,
        priority_score=priority,
        score_components={"operator_priority": 10.0, "age": 2.0},
    )


def test_build_prompt_is_deterministic() -> None:
    item = _make_item()
    a = build_prompt(item, [item])
    b = build_prompt(item, [item])
    assert a == b
    assert item.goal in a
    assert "operator_priority" in a


@pytest.mark.asyncio
async def test_generate_returns_marker_when_no_adapter() -> None:
    item = _make_item()
    result = await generate_rationale(item, [item], adapter=None)
    assert result == UNAVAILABLE_MARKER


@pytest.mark.asyncio
async def test_generate_returns_adapter_text_on_success() -> None:
    item = _make_item()

    async def mock(prompt: str) -> str:
        return "Picked because operator_priority dominates the score."

    result = await generate_rationale(item, [item], adapter=mock)
    assert "operator_priority" in result


@pytest.mark.asyncio
async def test_generate_returns_marker_on_timeout() -> None:
    item = _make_item()

    async def slow(prompt: str) -> str:
        await asyncio.sleep(5)
        return "never reached"

    result = await generate_rationale(
        item, [item], adapter=slow, timeout_seconds=0.05
    )
    assert result == UNAVAILABLE_MARKER


@pytest.mark.asyncio
async def test_generate_returns_marker_on_exception() -> None:
    item = _make_item()

    async def broken(prompt: str) -> str:
        raise RuntimeError("adapter down")

    result = await generate_rationale(item, [item], adapter=broken)
    assert result == UNAVAILABLE_MARKER


@pytest.mark.asyncio
async def test_generate_caps_output_length() -> None:
    item = _make_item()

    async def runaway(prompt: str) -> str:
        return "x" * 100_000

    result = await generate_rationale(
        item, [item], adapter=runaway, max_output_chars=2000
    )
    assert len(result) == 2000
