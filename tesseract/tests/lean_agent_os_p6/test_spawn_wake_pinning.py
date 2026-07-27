"""P6 Task 2 §G2 pinning test.

Design: Docs/Plan/lean-agent-os/idle-wake-design.md §G2 — reclassified
non-defect after reviewer verification. `session.current_turn_task` is a
computed property over `current_turn_tasks[active_chat_id]`
(`session.py:370-381`). An operator message arriving while a wake turn is
in flight on the active chat routes through `_start_turn`'s in-flight
branch — the operator's message joins the FIFO queue rather than spawning
a second, duplicate turn.

conversation-layer Task 4.2 (Q2) updated this test: the in-flight branch
now queues the follow-up as a normal turn (`chat_queues`) instead of the
old mid-turn injection default — see `turn_intake._start_turn`. Mid-turn
inject is reserved for the future Q3 steer command.

This test pins the "no duplicate turn" behavior for the wake-turn scenario
specifically: a `spawn_wake:*`-named task occupies `current_turn_task`
when the operator's message arrives.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from tesseract.mirror.server import ws as ws_module
from tesseract.mirror.server.session import ServerSession


def _build_app() -> web.Application:
    app = web.Application()
    app["mood"] = None
    app["adapter_options"] = SimpleNamespace(tier="api")
    policy = MagicMock()
    policy.resolve_posture.return_value = "auto"
    app["config"] = SimpleNamespace(permissions=policy)
    app["tts_engine"] = None
    app["voice_state"] = None
    return app


def _make_session(session_id: str = "sess-wake-pin") -> ServerSession:
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.closed = False
    pending_injected: list[dict] = []

    def _enqueue(text: str) -> None:
        text = (text or "").strip()
        if text:
            pending_injected.append({"text": text, "queued_at": "stub"})

    chat_session = SimpleNamespace(
        tool_context=SimpleNamespace(cancel_event=asyncio.Event()),
        pending_injected_messages=pending_injected,
        enqueue_user_inject=_enqueue,
    )
    return ServerSession(
        session_id=session_id,
        ws=ws,
        chat_session=chat_session,  # type: ignore[arg-type]
        event_log=MagicMock(append=MagicMock()),
    )


@pytest.mark.asyncio
async def test_operator_message_during_inflight_wake_turn_queues_no_duplicate() -> None:
    """A spawn-wake turn is running on the active chat. An operator message
    arrives: it must (a) join the FIFO queue as a normal turn (Q2), NOT a
    mid-turn injection, (b) NOT spawn a second task, and (c) leave the
    original wake task as the chat's one and only in-flight turn."""
    app = _build_app()
    session = _make_session()

    async def _wake_turn_stub() -> None:
        await asyncio.sleep(60)

    wake_task = asyncio.create_task(
        _wake_turn_stub(), name=f"spawn_wake:{session.session_id}:{session.active_chat_id}",
    )
    session.current_turn_task = wake_task
    try:
        await ws_module._start_turn(app, session, {"text": "operator follow-up"})

        # (a) FIFO queue path taken — the operator's text landed on
        # `chat_queues`, not the ChatSession's mid-turn inject queue.
        injected = session.chat_session.pending_injected_messages
        assert injected == []
        cid = session.active_chat_id
        assert [e["text"] for e in session.chat_queues[cid]] == ["operator follow-up"]

        # (b)/(c) single turn — current_turn_task is still the SAME wake
        # task object; no second task was spawned for this chat.
        assert session.current_turn_task is wake_task
        assert not wake_task.done()
        assert len(session.current_turn_tasks) == 1
    finally:
        wake_task.cancel()
        try:
            await wake_task
        except asyncio.CancelledError:
            pass
