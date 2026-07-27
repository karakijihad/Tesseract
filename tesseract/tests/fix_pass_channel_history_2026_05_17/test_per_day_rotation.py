"""2026-05-17 — per-day conversation rotation, history reader, log fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.integrations._channel_adapter import ChannelMessage
from tesseract.integrations._conversation_store import ConversationStore


def _msg(ts: str, direction: str, body: str) -> ChannelMessage:
    return ChannelMessage(ts=ts, direction=direction, body=body, extra={})


@pytest.mark.asyncio
async def test_append_routes_to_per_day_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = ConversationStore()
    store.append("telegram", "99", _msg("2026-05-13T12:00:00+00:00", "inbound", "a"))
    store.append("telegram", "99", _msg("2026-05-14T08:00:00+00:00", "inbound", "b"))
    store.append("telegram", "99", _msg("2026-05-14T20:00:00+00:00", "outbound", "c"))

    day_dir = tmp_path / "logs" / "channels" / "telegram" / "99" / "conversations"
    assert (day_dir / "2026-05-13.jsonl").exists()
    assert (day_dir / "2026-05-14.jsonl").exists()
    rows_14 = (day_dir / "2026-05-14.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows_14) == 2


@pytest.mark.asyncio
async def test_tail_walks_per_day_newest_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = ConversationStore()
    store.append("telegram", "99", _msg("2026-05-13T12:00:00+00:00", "inbound", "old"))
    store.append("telegram", "99", _msg("2026-05-14T08:00:00+00:00", "inbound", "newer"))
    store.append("telegram", "99", _msg("2026-05-15T08:00:00+00:00", "inbound", "newest"))

    rows = store.tail("telegram", "99", limit=10)
    assert [r["body"] for r in rows] == ["newest", "newer", "old"]


@pytest.mark.asyncio
async def test_day_rows_returns_chronological(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = ConversationStore()
    store.append("telegram", "99", _msg("2026-05-14T08:00:00+00:00", "inbound", "first"))
    store.append("telegram", "99", _msg("2026-05-14T20:00:00+00:00", "outbound", "second"))
    rows = store.day_rows("telegram", "99", date="2026-05-14")
    assert [r["body"] for r in rows] == ["first", "second"]


@pytest.mark.asyncio
async def test_list_days_newest_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = ConversationStore()
    store.append("telegram", "99", _msg("2026-05-13T12:00:00+00:00", "inbound", "a"))
    store.append("telegram", "99", _msg("2026-05-15T12:00:00+00:00", "inbound", "b"))
    days = store.list_days("telegram", "99")
    assert days == ["2026-05-15", "2026-05-13"]


@pytest.mark.asyncio
async def test_legacy_conversations_jsonl_still_readable(tmp_path, monkeypatch) -> None:
    """Backward-compat: a chat with the pre-rotation flat file still
    tails correctly until the migration script runs."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    chat_dir = tmp_path / "logs" / "channels" / "telegram" / "99"
    chat_dir.mkdir(parents=True, exist_ok=True)
    legacy = chat_dir / "conversations.jsonl"
    legacy.write_text(
        json.dumps({"ts": "2026-05-10T08:00:00+00:00", "direction": "inbound", "body": "legacy", "extra": {}, "attachments": []}) + "\n",
        encoding="utf-8",
    )

    store = ConversationStore()
    rows = store.tail("telegram", "99", limit=10)
    assert [r["body"] for r in rows] == ["legacy"]
