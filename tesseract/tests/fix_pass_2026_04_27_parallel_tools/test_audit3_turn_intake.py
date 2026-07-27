"""Turn-intake contract (audit-3 → 2026-05-01 voice-queueing →
2026-05-10 Phase 2 mid-turn inject → conversation-layer Task 4.2 Q2 FIFO):

#2 Typed = FIFO queue. While a turn is running, plain-text follow-ups
   (and payloads with attachments) append to `chat_queues[chat_id]` as a
   NORMAL turn and emit a `queued_message` envelope; `drain_next` pops
   them one at a time as each prior queued turn completes. The old
   Phase-2 default of pushing plain text onto `chat_session.
   pending_injected_messages` (mid-turn inject) is retired as the default
   — that path stays wired for the future Q3 steer command.

#3/#4 Voice = soft barge-in. `voice_cancel reason='barge_in'` only
   drops the server-side TTS chain so no further `tts_chunk` envelopes
   ship from the superseded turn. The chat brain turn keeps running so
   its compute is not wasted, and the new transcript routes through
   `voice_commit → _start_turn` which now lands on the same FIFO queue
   path as a typed follow-up.

#5 TTS streaming threshold raised to 600 — short/medium replies stay
   single-shot for uniform prosody. Cross-checked in
   `test_speak_streaming_batching.py`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web

from tesseract.mirror.server import ws as ws_module
from tesseract.mirror.server.session import ServerSession
from tesseract.mirror.server.voice_io import _handle_voice_cancel


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


def _make_session(session_id: str = "sess-q") -> ServerSession:
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.closed = False
    # Phase 2 (CLI parity): chat_session must expose pending_injected_messages
    # and enqueue_user_inject so `_start_turn`'s mid-turn inject branch
    # can target it. Stub them on the SimpleNamespace.
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


# ── #2 typed queue ────────────────────────────────────────────────


async def test_typed_message_queues_when_turn_running():
    """conversation-layer Task 4.2 (Q2) — while a turn is running, a
    plain-text follow-up is a NORMAL turn queued FIFO on
    `chat_queues[chat_id]`, not a mid-turn inject. Mid-turn inject
    (`pending_injected_messages`) is reserved for the future Q3 steer
    command and stays empty on this default path."""
    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id

    async def _busy() -> None:
        await asyncio.sleep(60)

    session.current_turn_task = asyncio.create_task(_busy())
    try:
        await ws_module._start_turn(app, session, {"text": "follow up"})
        assert [e["text"] for e in session.chat_queues[cid]] == ["follow up"]
        assert session.chat_session.pending_injected_messages == []
    finally:
        session.current_turn_task.cancel()
        try:
            await session.current_turn_task
        except asyncio.CancelledError:
            pass


async def test_typed_queue_accumulates_in_order():
    """conversation-layer Task 4.2 (Q2) — multiple plain-text follow-ups
    during the same turn each append to `chat_queues[chat_id]` in arrival
    order (FIFO, no last-wins coalescing)."""
    app = _build_app()
    session = _make_session()
    cid = session.active_chat_id

    async def _busy() -> None:
        await asyncio.sleep(60)

    session.current_turn_task = asyncio.create_task(_busy())
    try:
        await ws_module._start_turn(app, session, {"text": "first"})
        await ws_module._start_turn(app, session, {"text": "second"})
        assert [e["text"] for e in session.chat_queues[cid]] == ["first", "second"]
        assert session.chat_session.pending_injected_messages == []
    finally:
        session.current_turn_task.cancel()
        try:
            await session.current_turn_task
        except asyncio.CancelledError:
            pass


async def test_cancel_turn_clears_pending_typed():
    """Phase 2: cancel must drop BOTH the legacy attachment slot AND
    the new mid-turn inject queue. Operator stop / voice barge-in
    means "stop everything queued"; their live intent is whatever
    comes next."""
    session = _make_session()
    session.pending_user_text = "stale follow-up"
    session.chat_session.pending_injected_messages.append(
        {"text": "also stale", "queued_at": "stub"}
    )
    session.current_turn_task = None  # no active task to cancel
    # `_cancel_turn` signature drifted to (app, session) in a prior
    # session (the workspace event broadcaster needed the app handle);
    # the behaviour assertion below is the actual invariant under test.
    await ws_module._cancel_turn(_build_app(), session)
    assert session.pending_user_text is None
    assert session.chat_session.pending_injected_messages == []


# ── #3/#4 voice barge-in path ─────────────────────────────────────


async def test_voice_cancel_barge_in_preserves_running_turn_and_queue():
    """Voice queueing contract (2026-05-01) — `voice_cancel
    reason='barge_in'` is a SOFT barge-in: it drops the server-side
    TTS chain only. The chat brain turn keeps running (compute is not
    wasted), and any queued typed follow-up survives so the next turn
    boundary still drains it."""
    app = web.Application()
    app["scheduler"] = None
    session = _make_session()
    session.pending_user_text = "I typed this earlier"

    cancelled = asyncio.Event()

    async def _turn() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    turn_task = asyncio.create_task(_turn())
    session.current_turn_task = turn_task
    await asyncio.sleep(0)

    try:
        await _handle_voice_cancel(app, session, {"reason": "barge_in"})

        assert not cancelled.is_set(), "barge_in must NOT cancel the running turn"
        assert session.current_turn_task is turn_task
        assert session.pending_user_text == "I typed this earlier"
        assert not session.chat_session.tool_context.cancel_event.is_set()
    finally:
        turn_task.cancel()
        try:
            await turn_task
        except asyncio.CancelledError:
            pass


# ── #5 TTS streaming threshold ────────────────────────────────────


def test_tts_streaming_has_no_char_gate():
    """Speak mode now flushes complete sentence/paragraph segments
    immediately; voice consistency is protected by a per-turn voice
    snapshot instead of a character threshold."""
    assert not hasattr(ws_module, "_TTS_STREAMING_TRIGGER_CHARS")
