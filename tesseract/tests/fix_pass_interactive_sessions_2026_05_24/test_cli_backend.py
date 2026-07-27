from __future__ import annotations

import pytest
from tesseract.orchestrator.tars_controller.interactive.cli_backend import CliSessionBackend
from tesseract.orchestrator.tars_controller.interactive.types import SessionStatus


class _FakeAdapter:
    def __init__(self):
        self.calls = []

    async def run_turn(self, *, task, session_id, cwd, on_event, cancel_event=None, turn_timeout=None):
        self.calls.append((task, session_id))
        acc = _Acc(session_id or "sess-new", f"reply to {task}")
        on_event({"type": "assistant"})
        return acc


class _Acc:
    def __init__(self, sid, text):
        self.session_id = sid
        self.result_text = text
        self.usage = {}
        self.done = True
        self.is_error = False


@pytest.mark.asyncio
async def test_open_then_send_uses_resume():
    adapter = _FakeAdapter()
    events = []
    s = CliSessionBackend(
        handle="h1", target="claude", adapter=adapter, cwd=".",
        emit=lambda ev: events.append(ev),
    )
    r0 = await s.open("first task")
    assert r0.turn_index == 0
    assert r0.result_text == "reply to first task"
    assert r0.status is SessionStatus.DONE
    r1 = await s.send("second")
    assert r1.turn_index == 1
    assert adapter.calls[1][1] == "sess-new"   # resumed
    assert len(events) == 2                      # one emit per turn


@pytest.mark.asyncio
async def test_send_before_open_returns_error():
    adapter = _FakeAdapter()
    s = CliSessionBackend(
        handle="h2", target="claude", adapter=adapter, cwd=".",
        emit=lambda ev: None,
    )
    r = await s.send("message without open")
    assert r.is_error is True
    assert r.status is SessionStatus.ERROR
    assert adapter.calls == []   # adapter never called
