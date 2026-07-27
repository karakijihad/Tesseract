from __future__ import annotations

import pytest
from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.orchestrator.tars_controller.interactive.agent_backend import AgentSessionBackend
from tesseract.orchestrator.tars_controller.interactive.types import SessionStatus


class _FakeChatSession:
    def __init__(self) -> None:
        self.sends: list[str] = []

    async def send(self, text: str):
        self.sends.append(text)
        yield StreamChunk(type=ChunkType.TEXT, text=f"ans:{text}")
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")


class _FakeErrorSession:
    async def send(self, text: str):
        yield StreamChunk(type=ChunkType.ERROR, error="boom")


@pytest.mark.asyncio
async def test_open_then_send_reuses_session():
    sess = _FakeChatSession()
    events: list[dict] = []
    b = AgentSessionBackend(
        handle="h",
        target="researcher",
        chat_session=sess,
        emit=lambda ev: events.append(ev),
    )
    r0 = await b.open("task A")
    assert r0.result_text == "ans:task A"
    assert r0.status is SessionStatus.DONE
    assert r0.turn_index == 0

    r1 = await b.send("task B")
    assert r1.result_text == "ans:task B"
    assert sess.sends == ["task A", "task B"]   # same session, two turns
    assert r1.turn_index == 1


@pytest.mark.asyncio
async def test_error_chunk_yields_error_status():
    events: list[dict] = []
    b = AgentSessionBackend(
        handle="h2",
        target="researcher",
        chat_session=_FakeErrorSession(),
        emit=lambda ev: events.append(ev),
    )
    r = await b.open("task X")
    assert r.status is SessionStatus.ERROR
    assert r.is_error is True
    assert any(e["type"] == "error" for e in events)


@pytest.mark.asyncio
async def test_send_after_close_returns_error():
    b = AgentSessionBackend(
        handle="h3",
        target="researcher",
        chat_session=_FakeChatSession(),
        emit=lambda _: None,
    )
    await b.close()
    r = await b.send("anything")
    assert r.status is SessionStatus.ERROR
    assert r.is_error is True
