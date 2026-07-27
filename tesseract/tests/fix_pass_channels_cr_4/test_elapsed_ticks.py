"""CR-4 — ``_start_channel_turn`` fires elapsed-time progress pulses.

The phase contract: pulses at 15 / 30 / 60 / 120 s while the turn is
running, cancelled when the turn completes. We patch the
``ELAPSED_TICKS_S`` ladder to small values for speed, then drive
``_start_channel_turn`` with a fake chat_session whose ``send()``
holds for a controlled duration before emitting the final answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import pytest

from tesseract.integrations import _channel_progress as cp
from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.mirror.server import ws as ws_module


@dataclass
class _FakeToolContext:
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _FakeChatSession:
    chunks: list[StreamChunk]
    delay_between: float = 0.0
    tool_context: _FakeToolContext = field(default_factory=_FakeToolContext)
    history: list[dict[str, Any]] = field(default_factory=list)

    async def send(self, body: str) -> AsyncIterator[StreamChunk]:
        del body
        for ch in self.chunks:
            if self.delay_between:
                await asyncio.sleep(self.delay_between)
            yield ch
        # End-of-turn: record the assistant message so the post-turn
        # history scan can pull a reply for the empty-text path.
        self.history.append({"role": "assistant", "content": "ok"})


@dataclass
class _FakeSession:
    session_id: str
    chat_session: _FakeChatSession
    current_turn_task: asyncio.Task[None] | None = None


@pytest.mark.asyncio
async def test_elapsed_ticks_fire_at_scaled_intervals(monkeypatch):
    """Patch the tick ladder to 0.05/0.10/0.20s so the test runs fast;
    a turn that takes ~0.25s should see all three early ticks (the
    120s tick is unreachable in 0.25s and stays unfired)."""
    monkeypatch.setattr(cp, "ELAPSED_TICKS_S", (0.05, 0.10, 0.20, 5.0))
    # Patch the symbol referenced inside ws.py's nested import path.
    monkeypatch.setattr(
        "tesseract.mirror.server.ws.time.monotonic",
        ws_module.time.monotonic,
    )

    events: list[cp.ProgressEvent] = []

    async def _on_progress(event):
        events.append(event)

    chunks = [
        StreamChunk(type=ChunkType.TEXT, text="<answer>done</answer>"),
    ]
    chat = _FakeChatSession(chunks=chunks, delay_between=0.0)
    # Hold the stream open ~0.25s so three elapsed ticks fire before
    # the turn completes.
    original_send = chat.send

    async def slow_send(body):
        await asyncio.sleep(0.25)
        async for ch in original_send(body):
            yield ch

    chat.send = slow_send  # type: ignore[assignment]
    session = _FakeSession(session_id="s_tick", chat_session=chat)

    reply = await ws_module._start_channel_turn(
        app=None,
        session=session,
        channel="telegram",
        chat_id="99",
        body="hi",
        on_progress=_on_progress,
    )
    assert reply == "done"

    kinds = [(e.kind, round(e.elapsed_s, 2)) for e in events if e.kind == "elapsed"]
    # The 5.0s tick must NOT have fired — the turn finished before it.
    assert all(t < 5.0 for _, t in kinds), kinds
    # At least one early tick must have fired (we slept 0.25s, ladder
    # starts at 0.05s).
    assert any(t in (0.05, 0.1, 0.2) for _, t in kinds), kinds


@pytest.mark.asyncio
async def test_elapsed_ticks_cancelled_on_completion(monkeypatch):
    """A turn that completes before any tick must not surface any
    elapsed event — confirms the cancellation path works (no ghost
    edits after the final reply)."""
    monkeypatch.setattr(cp, "ELAPSED_TICKS_S", (5.0, 10.0))
    events: list[cp.ProgressEvent] = []

    async def _on_progress(event):
        events.append(event)

    chunks = [StreamChunk(type=ChunkType.TEXT, text="<answer>fast</answer>")]
    chat = _FakeChatSession(chunks=chunks)
    session = _FakeSession(session_id="s_fast", chat_session=chat)

    await ws_module._start_channel_turn(
        app=None,
        session=session,
        channel="telegram",
        chat_id="99",
        body="hi",
        on_progress=_on_progress,
    )
    # Wait a beat to make sure no rogue tick task survived.
    await asyncio.sleep(0.1)
    assert [e for e in events if e.kind == "elapsed"] == []
