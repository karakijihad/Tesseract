"""Session 1 (2026-05-16) — chat memory tier (summary + reflection + recall).

Asserts that :class:`ChatMemoryService` correctly:
1. Maintains a rolling per-chat summary on ``append_evictions`` —
   newest-first, capped, header preserved.
2. Skips reflection cleanly when no MemoryBundle is wired.
3. Writes a reflection via ``store.write`` after the deferred timer fires,
   tagged with channel + chat:<id> so recall can find it.
4. Is idempotent — calling reflect again with no new turns is a no-op.
5. ``recall_for_inbound`` returns the rolling summary when no bundle is
   wired, and includes prior tagged memories when one is.
6. ``on_turn_completed`` cancels prior pending reflection tasks.
7. ``shutdown`` cancels every outstanding reflection task.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tesseract.integrations._chat_memory import ChatMemoryService
from tesseract.integrations._conversation_store import ConversationStore


@dataclass
class _FakeMemoryBundle:
    store: Any
    index: Any
    embeddings: Any
    pipeline: Any


class _RecordingStore:
    """Captures every ``write`` call so tests can assert against frontmatter."""

    def __init__(self) -> None:
        self.writes: list[tuple[Any, str]] = []

    def write(self, frontmatter, body, *, subdir_override=None):
        del subdir_override
        self.writes.append((frontmatter, body))
        return True


class _StubIndex:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    def add_or_update(self, frontmatter) -> None:
        self.updates.append(frontmatter)


class _StubPipeline:
    def __init__(self, packets) -> None:
        self.calls: list[str] = []
        self._packets = packets

    async def retrieve(self, query, type_filter=None, top_k=5):
        del type_filter, top_k
        self.calls.append(query)
        return self._packets


@dataclass
class _FakeResult:
    title: str
    body: str
    tags: list[str]


@dataclass
class _FakePacket:
    results: list[_FakeResult]
    synthesis: str = ""


# -- summary tests --------------------------------------------------------


@pytest.mark.asyncio
async def test_append_evictions_writes_summary_newest_first(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    service = ChatMemoryService(conversation_store=ConversationStore())

    # First eviction batch — three rows oldest→newest.
    service.append_evictions(
        "telegram",
        "99",
        [
            {"role": "user", "content": "first thing"},
            {"role": "assistant", "content": "first reply"},
        ],
    )
    # Second batch — should land NEWER than the first.
    service.append_evictions(
        "telegram",
        "99",
        [
            {"role": "user", "content": "second thing"},
        ],
    )

    text = service.read_summary("telegram", "99")
    assert text.startswith("# Rolling summary — telegram:99"), text[:200]
    bullets = [ln for ln in text.splitlines() if ln.startswith("- ")]
    # Newest-first ordering: "second thing" is the newest, must be at top.
    assert bullets[0] == "- **user**: second thing"
    assert "first thing" in bullets[-2]
    assert "first reply" in bullets[-1]


@pytest.mark.asyncio
async def test_append_evictions_truncates_long_content(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    service = ChatMemoryService(conversation_store=ConversationStore())

    long_text = "x" * 500
    service.append_evictions(
        "telegram", "99",
        [{"role": "user", "content": long_text}],
    )
    text = service.read_summary("telegram", "99")
    bullet = [ln for ln in text.splitlines() if ln.startswith("- ")][0]
    # Cap is 280 chars + trailing "…"; plus the role prefix.
    assert "x" * 280 not in bullet  # full string never lands
    assert "…" in bullet


# -- reflection tests -----------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_no_op_without_memory_bundle(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    convo = ConversationStore()
    service = ChatMemoryService(conversation_store=convo, memory_bundle=None)
    # Should not raise even when convo has no rows.
    await service._write_reflection("telegram", "99")


@pytest.mark.asyncio
async def test_reflection_writes_tagged_memory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    convo = ConversationStore()
    # Seed two rows via the real store so the service reads back what it
    # would in production.
    from tesseract.integrations._channel_adapter import ChannelMessage

    convo.append("telegram", "99", ChannelMessage(
        ts="2026-05-16T10:00:00+00:00",
        direction="inbound",
        body="what's on the agenda?",
        extra={},
    ))
    convo.append("telegram", "99", ChannelMessage(
        ts="2026-05-16T10:00:05+00:00",
        direction="outbound",
        body="three things: foo, bar, baz",
        extra={},
    ))

    store = _RecordingStore()
    index = _StubIndex()
    bundle = _FakeMemoryBundle(store=store, index=index, embeddings=None, pipeline=None)
    service = ChatMemoryService(conversation_store=convo, memory_bundle=bundle)

    await service._write_reflection("telegram", "99")

    assert len(store.writes) == 1
    fm, body = store.writes[0]
    assert fm.type.value == "project"
    assert "channel" in fm.tags
    assert "telegram" in fm.tags
    assert "chat:99" in fm.tags
    assert "conversation-recap" in fm.tags
    assert "what's on the agenda?" in body
    assert "three things: foo, bar, baz" in body


@pytest.mark.asyncio
async def test_reflection_is_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    convo = ConversationStore()
    from tesseract.integrations._channel_adapter import ChannelMessage

    convo.append("telegram", "99", ChannelMessage(
        ts="2026-05-16T10:00:00+00:00",
        direction="inbound",
        body="hi",
        extra={},
    ))

    store = _RecordingStore()
    bundle = _FakeMemoryBundle(
        store=store, index=_StubIndex(), embeddings=None, pipeline=None,
    )
    service = ChatMemoryService(conversation_store=convo, memory_bundle=bundle)

    await service._write_reflection("telegram", "99")
    await service._write_reflection("telegram", "99")  # no new turns
    assert len(store.writes) == 1


@pytest.mark.asyncio
async def test_reflection_blocked_write_does_not_retry(
    tmp_path, monkeypatch
) -> None:
    """A policy-blocked write (store.write → False) must advance the
    reflection marker — otherwise the */5 sweep retries the identical
    blocked write forever (live pathology observed 2026-07-12)."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    convo = ConversationStore()
    from tesseract.integrations._channel_adapter import ChannelMessage

    convo.append("telegram", "99", ChannelMessage(
        ts="2026-05-16T10:00:00+00:00",
        direction="inbound",
        body="hi",
        extra={},
    ))

    class _BlockingStore(_RecordingStore):
        def write(self, frontmatter, body, *, subdir_override=None):
            super().write(frontmatter, body, subdir_override=subdir_override)
            return False

    store = _BlockingStore()
    bundle = _FakeMemoryBundle(
        store=store, index=_StubIndex(), embeddings=None, pipeline=None,
    )
    service = ChatMemoryService(conversation_store=convo, memory_bundle=bundle)

    await service._write_reflection("telegram", "99")
    await service._write_reflection("telegram", "99")  # sweep re-fires
    assert len(store.writes) == 1  # no retry of the same blocked recap

    # A genuinely new turn re-arms reflection for the new tail.
    convo.append("telegram", "99", ChannelMessage(
        ts="2026-05-16T11:00:00+00:00",
        direction="outbound",
        body="hello again",
        extra={},
    ))
    await service._write_reflection("telegram", "99")
    assert len(store.writes) == 2


# -- recall tests ---------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_returns_only_summary_without_bundle(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    service = ChatMemoryService(conversation_store=ConversationStore())
    service.append_evictions("telegram", "99",
        [{"role": "user", "content": "remember the auth refactor"}])

    out = await service.recall_for_inbound("telegram", "99", "what about auth")
    assert "ROLLING CHAT SUMMARY" in out
    assert "auth refactor" in out


@pytest.mark.asyncio
async def test_recall_includes_chat_tagged_memories(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    convo = ConversationStore()
    # One memory matches the chat tag, one does not — only the matched one
    # should land in the recall block.
    matched = _FakeResult(
        title="Yesterday's plan",
        body="ship the auth migration tomorrow",
        tags=["channel", "telegram", "chat:99", "conversation-recap"],
    )
    unrelated = _FakeResult(
        title="Unrelated soul notes",
        body="should not surface",
        tags=["soul"],
    )
    pipeline = _StubPipeline(_FakePacket(results=[matched, unrelated]))
    bundle = _FakeMemoryBundle(
        store=_RecordingStore(), index=_StubIndex(),
        embeddings=None, pipeline=pipeline,
    )
    service = ChatMemoryService(conversation_store=convo, memory_bundle=bundle)

    out = await service.recall_for_inbound("telegram", "99", "auth")
    assert "PRIOR CHAT MEMORIES" in out
    assert "ship the auth migration tomorrow" in out
    assert "should not surface" not in out  # filtered by tag
    assert pipeline.calls and "chat:99" in pipeline.calls[0]


# -- scheduling tests -----------------------------------------------------


@pytest.mark.asyncio
async def test_on_turn_completed_updates_last_turn_marker(
    tmp_path, monkeypatch
) -> None:
    """2026-05-17 — reflection now rides the scheduler, not an in-process
    timer. ``on_turn_completed`` only records the last-turn timestamp;
    the actual reflection fires from ``ChannelReflectionSweepJob``."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    service = ChatMemoryService(
        conversation_store=ConversationStore(),
        reflection_delay_s=60,
    )
    await service.on_turn_completed("telegram", "99")
    key = ("telegram", "99")
    assert key in service._last_turn
    first_ts = service._last_turn[key]
    await service.on_turn_completed("telegram", "99")
    assert service._last_turn[key] >= first_ts
    # No in-process tasks scheduled anymore.
    assert service._reflect_tasks == {}


@pytest.mark.asyncio
async def test_shutdown_drains_any_legacy_tasks(
    tmp_path, monkeypatch
) -> None:
    """Legacy callers that manually queue into ``_reflect_tasks`` are
    drained on shutdown so a long-running process doesn't leak."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    service = ChatMemoryService(
        conversation_store=ConversationStore(),
        reflection_delay_s=60,
    )

    async def _placeholder() -> None:
        await asyncio.sleep(60)

    service._reflect_tasks[("telegram", "99")] = asyncio.create_task(_placeholder())
    await service.shutdown()
    assert service._reflect_tasks == {}
