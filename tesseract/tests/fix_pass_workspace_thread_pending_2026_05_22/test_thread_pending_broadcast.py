"""workspace_thread_pending — live WS envelope for the CommentThread indicator.

The synthetic-workspace-turn lifecycle in ``ws.py`` emits this envelope so the
CommentThread renders `tars · thinking…` (state="thinking"), `tars · queued…`
(state="queued"), or removes the row (state="cleared"). This test pins the
envelope shape + delivery so a future refactor can't silently drop it.
"""

from __future__ import annotations

from typing import Any

import pytest


class _StubWS:
    closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        self._last = payload


class _StubSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.event_log: list[dict[str, Any]] = []
        self.ws = _StubWS()


@pytest.mark.asyncio
async def test_thread_pending_envelope_shape() -> None:
    from tesseract.workspace_events.broadcast import broadcast_thread_pending

    sess = _StubSession("s1")
    app = {"server_sessions": {"s1": sess}}

    await broadcast_thread_pending(
        app, event_id="evt_abc", comment_id="cmt_xyz", state="thinking",
    )

    assert len(sess.event_log) == 1
    env = sess.event_log[0]
    assert env["type"] == "workspace_thread_pending"
    assert env["category"] == "workspace"
    assert env["data"] == {
        "event_id": "evt_abc",
        "comment_id": "cmt_xyz",
        "state": "thinking",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "thinking", "cleared"])
async def test_thread_pending_state_passthrough(state: str) -> None:
    from tesseract.workspace_events.broadcast import broadcast_thread_pending

    sess = _StubSession("s1")
    app = {"server_sessions": {"s1": sess}}

    await broadcast_thread_pending(
        app, event_id="evt_1", comment_id="cmt_1", state=state,
    )

    assert sess.event_log[0]["data"]["state"] == state


@pytest.mark.asyncio
async def test_thread_pending_no_sessions_is_noop() -> None:
    """REPL / standalone — no Mirror sessions attached, must not raise."""
    from tesseract.workspace_events.broadcast import broadcast_thread_pending

    await broadcast_thread_pending(
        app={"server_sessions": {}},
        event_id="evt_1", comment_id="cmt_1", state="thinking",
    )
    await broadcast_thread_pending(
        app=None,
        event_id="evt_1", comment_id="cmt_1", state="thinking",
    )


@pytest.mark.asyncio
async def test_thread_pending_fan_out_to_every_session() -> None:
    """Two open tabs both receive the envelope."""
    from tesseract.workspace_events.broadcast import broadcast_thread_pending

    a = _StubSession("a")
    b = _StubSession("b")
    app = {"server_sessions": {"a": a, "b": b}}

    await broadcast_thread_pending(
        app, event_id="evt_1", comment_id="cmt_1", state="queued",
    )

    assert len(a.event_log) == 1
    assert len(b.event_log) == 1
    assert a.event_log[0]["type"] == "workspace_thread_pending"
    assert b.event_log[0]["type"] == "workspace_thread_pending"


@pytest.mark.asyncio
async def test_cancel_turn_leaves_workspace_queues_intact(monkeypatch) -> None:
    """WP-2: ``_cancel_turn`` only cancels the chat turn. Workspace
    synthetic turns live on independent threads — the operator hitting
    Stop on the chat panel must NOT abandon queued workspace replies
    or kill in-flight synthetic turns.

    Replaces the earlier ``test_cancel_turn_clears_queued_workspace_indicators``
    (Session B regression for the pre-WP-2 single-lane model). Under WP-2
    the lanes are independent, so the cancel-clear semantics change.
    """
    import asyncio
    from collections import deque
    from types import SimpleNamespace

    from tesseract.mirror.server import ws as ws_mod

    broadcasts: list[dict] = []

    async def fake_broadcast_thread_pending(app, *, event_id, comment_id, state):
        broadcasts.append({
            "event_id": event_id, "comment_id": comment_id, "state": state,
        })

    monkeypatch.setattr(
        "tesseract.workspace_events.broadcast.broadcast_thread_pending",
        fake_broadcast_thread_pending,
    )

    tool_context = SimpleNamespace(cancel_event=asyncio.Event())
    chat_session = SimpleNamespace(
        tool_context=tool_context, pending_injected_messages=[],
    )

    queued = deque([
        {"workspace_origin": {"event_id": "evt_A", "comment_id": "cmt_A"}},
        {"workspace_origin": {"event_id": "evt_B", "comment_id": "cmt_B"}},
    ])
    synthetic_running = {"evt_C": asyncio.create_task(asyncio.sleep(10))}

    session = SimpleNamespace(
        session_id="s1",
        active_chat_id="c1",
        chat_queues={"c1": deque([{"text": "drop me"}])},
        pending_workspace_payloads=queued,
        synthetic_turn_tasks=synthetic_running,
        chat_session=chat_session,
        current_turn_task=None,
        tts_synth_task=None,
        tts_buffer="",
        voice_pcm_buffer=None,
        ws=_StubWS(),
        event_log=[],
    )

    monkeypatch.setattr("tesseract.mirror.server.turn_intake._cancel_tts_output", lambda s: None)

    app = {"server_sessions": {"s1": session}}
    try:
        await ws_mod._cancel_turn(app, session)

        # Queue intact — workspace lane unaffected by chat cancel.
        assert len(session.pending_workspace_payloads) == 2
        # No `cleared` envelopes — those threads are still pending.
        cleared = [b for b in broadcasts if b["state"] == "cleared"]
        assert cleared == []
        # In-flight synthetic turn left running.
        assert "evt_C" in session.synthetic_turn_tasks
        assert not session.synthetic_turn_tasks["evt_C"].done()
        # Chat-side state still cleared (chat_queues entry popped).
        assert "c1" not in session.chat_queues
    finally:
        synthetic_running["evt_C"].cancel()
        try:
            await synthetic_running["evt_C"]
        except asyncio.CancelledError:
            pass
