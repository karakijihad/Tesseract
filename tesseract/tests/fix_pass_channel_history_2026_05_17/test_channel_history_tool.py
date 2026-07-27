"""2026-05-17 — channel_history_read kernel tool."""

from __future__ import annotations

import pytest

from tesseract.integrations._channel_adapter import ChannelMessage
from tesseract.integrations._conversation_store import ConversationStore
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.channel_history import (
    ChannelHistoryReadInput,
    ChannelHistoryReadTool,
)


def _seed(store: ConversationStore) -> None:
    rows = [
        ("2026-05-14T08:00:00+00:00", "inbound", "what about the trading bot?"),
        ("2026-05-14T08:01:00+00:00", "outbound", "let's build it on Binance USDT"),
        ("2026-05-15T08:00:00+00:00", "inbound", "good morning"),
        ("2026-05-15T08:01:00+00:00", "outbound", "morning, jane"),
        ("2026-05-16T08:00:00+00:00", "inbound", "what was the trading plan again"),
    ]
    for ts, dirn, body in rows:
        store.append("telegram", "99", ChannelMessage(ts=ts, direction=dirn, body=body, extra={}))


@pytest.mark.asyncio
async def test_date_mode_returns_one_day(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    _seed(ConversationStore())

    tool = ChannelHistoryReadTool()
    res = await tool.run(
        ChannelHistoryReadInput(chat_ref="99", date="2026-05-14"),
        ToolContext(),
    )
    assert not res.is_error
    assert res.metadata == {"rows_returned": 2}
    assert "trading bot" in res.output
    assert "Binance USDT" in res.output


@pytest.mark.asyncio
async def test_days_back_default_returns_recent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    _seed(ConversationStore())

    tool = ChannelHistoryReadTool()
    # days_back=1 returns just the newest day (2026-05-16).
    res = await tool.run(
        ChannelHistoryReadInput(chat_ref="99", days_back=1),
        ToolContext(),
    )
    assert not res.is_error
    assert "trading plan again" in res.output
    # Older days should NOT appear.
    assert "good morning" not in res.output


@pytest.mark.asyncio
async def test_substring_search_with_context(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    _seed(ConversationStore())

    tool = ChannelHistoryReadTool()
    res = await tool.run(
        ChannelHistoryReadInput(chat_ref="99", substring="trading", context_rows=1),
        ToolContext(),
    )
    assert not res.is_error
    # Both trading mentions land + their context.
    assert "trading bot" in res.output
    assert "trading plan" in res.output


@pytest.mark.asyncio
async def test_empty_chat_returns_friendly_no_rows(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    tool = ChannelHistoryReadTool()
    res = await tool.run(
        ChannelHistoryReadInput(chat_ref="999"),
        ToolContext(),
    )
    assert not res.is_error
    assert "no rows" in res.output.lower()
