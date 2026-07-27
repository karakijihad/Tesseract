"""2026-05-17 — scheduler-driven reflection sweep + log-tail recall fallback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tesseract.integrations._channel_adapter import ChannelMessage
from tesseract.integrations._chat_memory import ChatMemoryService
from tesseract.integrations._conversation_store import ConversationStore


@dataclass
class _FakeMemoryBundle:
    store: Any
    index: Any
    embeddings: Any
    pipeline: Any


class _RecordingStore:
    def __init__(self) -> None:
        self.writes: list[tuple[Any, str]] = []

    def write(self, fm, body, *, subdir_override=None):
        del subdir_override
        self.writes.append((fm, body))
        return True


class _StubIndex:
    def add_or_update(self, fm) -> None:
        del fm


class _StubPipeline:
    """Pipeline that returns no chat-tagged hits → triggers log fallback."""

    async def retrieve(self, query, type_filter=None, top_k=5):
        del query, type_filter, top_k

        @dataclass
        class _Packet:
            results: list = None  # type: ignore[assignment]
            synthesis: str = ""

        return _Packet(results=[])


# -- log-fallback recall --------------------------------------------------


@pytest.mark.asyncio
async def test_recall_falls_back_to_log_when_memory_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    convo = ConversationStore()
    convo.append("telegram", "99", ChannelMessage(
        ts="2026-05-16T11:00:00+00:00", direction="inbound",
        body="let's build a trading bot", extra={},
    ))
    convo.append("telegram", "99", ChannelMessage(
        ts="2026-05-16T11:01:00+00:00", direction="outbound",
        body="on Binance, USDT pairs", extra={},
    ))

    bundle = _FakeMemoryBundle(
        store=_RecordingStore(), index=_StubIndex(),
        embeddings=None, pipeline=_StubPipeline(),
    )
    service = ChatMemoryService(conversation_store=convo, memory_bundle=bundle)

    out = await service.recall_for_inbound("telegram", "99", "trading")
    assert "RECENT CONVERSATION" in out
    assert "trading bot" in out
    assert "Binance, USDT pairs" in out


@pytest.mark.asyncio
async def test_recall_log_fallback_caps_at_tail_size(tmp_path, monkeypatch) -> None:
    """30-turn cap keeps the prompt budget bounded even when the chat
    has thousands of historical rows."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    convo = ConversationStore()
    for i in range(60):
        convo.append("telegram", "99", ChannelMessage(
            ts=f"2026-05-16T11:{i:02d}:00+00:00",
            direction=("inbound" if i % 2 == 0 else "outbound"),
            body=f"msg-{i}", extra={},
        ))

    bundle = _FakeMemoryBundle(
        store=_RecordingStore(), index=_StubIndex(),
        embeddings=None, pipeline=_StubPipeline(),
    )
    service = ChatMemoryService(conversation_store=convo, memory_bundle=bundle)
    out = await service.recall_for_inbound("telegram", "99", "anything")

    # Should have included only the last 30 rows; the oldest msg-0 / msg-1 must not appear.
    assert "msg-59" in out
    assert "msg-30" in out
    assert "msg-0\n" not in out
    assert "msg-1\n" not in out


# -- scheduler-driven reflection sweep -----------------------------------


@pytest.mark.asyncio
async def test_reflection_sweep_fires_on_idle_chat(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    convo = ConversationStore()
    # Seed one inbound + the matching state.json mention.
    convo.append("telegram", "99", ChannelMessage(
        ts="2026-05-16T11:00:00+00:00", direction="inbound",
        body="trading bot plan", extra={},
    ))

    store = _RecordingStore()
    bundle = _FakeMemoryBundle(
        store=store, index=_StubIndex(),
        embeddings=None, pipeline=None,
    )
    chat_memory = ChatMemoryService(
        conversation_store=convo, memory_bundle=bundle, reflection_delay_s=60,
    )

    # Fake adapter exposing _chat_memory + _state so the sweep can find it.
    adapter = MagicMock()
    adapter.name = "telegram"
    adapter._chat_memory = chat_memory
    state = MagicMock()
    # last_message_ts > delay ago (idle by an hour, delay is 60 s).
    state.poll_state.last_message_ts = {
        "99": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }
    adapter._state = state

    from tesseract.integrations import register_channel, unregister_channel
    register_channel.__wrapped__ if False else None  # noqa — appease lint without import-side-effect
    # Make sure adapter satisfies the runtime-checkable Protocol.
    from unittest.mock import AsyncMock
    adapter.start = AsyncMock(); adapter.stop = AsyncMock()
    adapter.status_snapshot = MagicMock(); adapter.list_users = MagicMock(return_value=[])
    adapter.approve = AsyncMock(); adapter.revoke = AsyncMock(); adapter.block = AsyncMock()
    adapter.list_conversation = MagicMock(return_value=[])
    register_channel(adapter)
    try:
        from tesseract.scheduler.tasks.channel_reflection_sweep import (
            ChannelReflectionSweepJob,
        )
        from tesseract.scheduler.types import JobContext

        job = ChannelReflectionSweepJob()
        result = await job.run(JobContext(
            job_name="channel_reflection_sweep",
            config={"reflection_delay_s": 60},
        ))
        assert result.ok
        assert result.payload["fired"] == 1
        assert len(store.writes) == 1
        fm, body = store.writes[0]
        assert "chat:99" in fm.tags
        assert "trading bot plan" in body
    finally:
        unregister_channel("telegram")


@pytest.mark.asyncio
async def test_reflection_sweep_skips_recent_chat(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    convo = ConversationStore()
    store = _RecordingStore()
    bundle = _FakeMemoryBundle(
        store=store, index=_StubIndex(),
        embeddings=None, pipeline=None,
    )
    chat_memory = ChatMemoryService(
        conversation_store=convo, memory_bundle=bundle, reflection_delay_s=1800,
    )

    adapter = MagicMock()
    adapter.name = "telegram"
    adapter._chat_memory = chat_memory
    state = MagicMock()
    state.poll_state.last_message_ts = {
        "99": datetime.now(timezone.utc).isoformat(),  # JUST happened
    }
    adapter._state = state

    from unittest.mock import AsyncMock
    adapter.start = AsyncMock(); adapter.stop = AsyncMock()
    adapter.status_snapshot = MagicMock(); adapter.list_users = MagicMock(return_value=[])
    adapter.approve = AsyncMock(); adapter.revoke = AsyncMock(); adapter.block = AsyncMock()
    adapter.list_conversation = MagicMock(return_value=[])

    from tesseract.integrations import register_channel, unregister_channel
    register_channel(adapter)
    try:
        from tesseract.scheduler.tasks.channel_reflection_sweep import (
            ChannelReflectionSweepJob,
        )
        from tesseract.scheduler.types import JobContext

        job = ChannelReflectionSweepJob()
        result = await job.run(JobContext(
            job_name="channel_reflection_sweep",
            config={"reflection_delay_s": 1800},
        ))
        assert result.payload["fired"] == 0
        assert result.payload["skipped_idle"] == 1
        assert store.writes == []
    finally:
        unregister_channel("telegram")
