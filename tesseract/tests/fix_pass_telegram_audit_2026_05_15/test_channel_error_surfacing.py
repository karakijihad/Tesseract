"""Audit follow-up — channel turn errors land as a visible reply.

Pre-fix, ``_start_channel_turn`` returned ``None`` when ``error_holder``
held a tool-iteration-cap message or an adapter crash; the bridge then
showed "(no reply produced this turn)" — the operator on the phone had
no idea why TARS went silent.

The fix surfaces the error text (with an operator/friend tier redaction).
Tests pin three contracts:

1. Operator-tier turn → full error text surfaced.
2. Friend-tier turn → generic redacted suffix; raw exception string stays
   in the backend log (verified by absence in the returned reply).
3. Partial reply + error → both are present in the returned text.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_error_holder_surfaces_for_operator_tier(monkeypatch) -> None:
    """Tool-iteration-cap message must reach the user verbatim."""
    from tesseract.mirror.server import ws as ws_mod

    async def fake_send(body):
        # Yield ERROR chunk so error_holder fills.
        from tesseract.brain.chat import StreamChunk, ChunkType

        yield StreamChunk(
            type=ChunkType.ERROR,
            error="Response truncated — tool iteration cap reached (10).",
        )

    chat_session = MagicMock()
    chat_session.send = fake_send
    chat_session.tool_context = MagicMock(cancel_event=MagicMock())
    chat_session.tool_context.cancel_event.clear = MagicMock()
    chat_session.history = []

    session = MagicMock()
    session.session_id = "telegram_111_abc"
    session.chat_session = chat_session
    session.current_turn_task = None
    session.channel_tier = "operator"

    app = MagicMock()
    reply = await ws_mod._start_channel_turn(
        app, session, channel="telegram", chat_id="111", body="hi", on_progress=None
    )
    assert reply is not None
    assert "⚠" in reply
    assert "tool iteration cap" in reply.lower()


@pytest.mark.asyncio
async def test_error_holder_redacted_for_friend_tier(monkeypatch) -> None:
    """Friend tier sees a generic message — no exception class / paths."""
    from tesseract.mirror.server import ws as ws_mod

    async def fake_send(body):
        from tesseract.brain.chat import StreamChunk, ChunkType

        yield StreamChunk(
            type=ChunkType.ERROR,
            error="adapter crashed: KeyError: '/secret/internal/path'",
        )

    chat_session = MagicMock()
    chat_session.send = fake_send
    chat_session.tool_context = MagicMock(cancel_event=MagicMock())
    chat_session.tool_context.cancel_event.clear = MagicMock()
    chat_session.history = []

    session = MagicMock()
    session.session_id = "telegram_222_xyz"
    session.chat_session = chat_session
    session.current_turn_task = None
    session.channel_tier = "friend"

    app = MagicMock()
    reply = await ws_mod._start_channel_turn(
        app, session, channel="telegram", chat_id="222", body="hi", on_progress=None
    )
    assert reply is not None
    assert "⚠" in reply
    # Internal detail must not leak to the friend.
    assert "KeyError" not in reply
    assert "/secret/internal" not in reply
    assert "adapter crashed" not in reply


@pytest.mark.asyncio
async def test_error_holder_appends_to_partial_reply(monkeypatch) -> None:
    """A partial answer plus an error → both are shown, error after."""
    from tesseract.mirror.server import ws as ws_mod

    async def fake_send(body):
        from tesseract.brain.chat import StreamChunk, ChunkType

        yield StreamChunk(type=ChunkType.TEXT, text="<answer>Partial work.</answer>")
        yield StreamChunk(
            type=ChunkType.ERROR,
            error="Response truncated — tool iteration cap reached (10).",
        )

    chat_session = MagicMock()
    chat_session.send = fake_send
    chat_session.tool_context = MagicMock(cancel_event=MagicMock())
    chat_session.tool_context.cancel_event.clear = MagicMock()
    chat_session.history = []

    session = MagicMock()
    session.session_id = "telegram_333_qq"
    session.chat_session = chat_session
    session.current_turn_task = None
    session.channel_tier = "operator"

    app = MagicMock()
    reply = await ws_mod._start_channel_turn(
        app, session, channel="telegram", chat_id="333", body="hi", on_progress=None
    )
    assert reply is not None
    assert "Partial work." in reply
    assert "⚠" in reply
    assert reply.index("Partial work.") < reply.index("⚠")
