"""CR-2: the outer ``try/except`` in ``TelegramBridge._decode_attachments``
catches decoder bugs that escape the per-kind handlers and surfaces them
as ``status="extract_failed"`` rather than crashing the poll loop.

Without this, a single AttributeError in a future handler refactor would
drop the inbound message and leave TARS unaware that something arrived
— the opposite of the visibility-first contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._channel_attachment import ChannelAttachment
from tesseract.integrations._channels_config import ChannelsConfig
from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.api import TelegramMessage
from tesseract.integrations.telegram.bridge import TelegramBridge
from tesseract.integrations.telegram.state import StateBundle, save_allowlist


def _build_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_chat_action = AsyncMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 5})
    bridge._api.edit_message_text = AsyncMock()
    bridge._conversations = MagicMock()
    bridge._app = {
        "channels_config": ChannelsConfig(),
        "stt_engine": None,
        "cost_ledger": None,
    }
    bridge._sessions = {}
    bridge._retention = RetentionPolicy.fallback()
    bridge._poll_task = None
    bridge._stop_event = MagicMock()
    bridge._started_at = ""
    bridge._last_poll_at = None
    bridge._error_count = 0

    state_dir = tmp_path / "telegram"
    state_dir.mkdir(parents=True, exist_ok=True)
    bridge._state = StateBundle(dir_path=state_dir)
    bridge._state.allowlist.chat_ids.add(99)
    save_allowlist(bridge._state.allowlist_path, bridge._state.allowlist)
    return bridge


@pytest.mark.asyncio
async def test_unexpected_decoder_exception_becomes_extract_failed_not_crash(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def broken_decode(att, message=None):
        del message
        raise RuntimeError("simulated decoder bug: dict has no attribute 'kind'")

    bridge._decode_attachment = broken_decode

    captured: dict = {}

    async def fake_start(app, session, *, channel, chat_id, body, on_progress=None):
        del app, session, channel, chat_id
        captured["body"] = body
        return ""

    monkeypatch.setattr(
        "tesseract.mirror.server.ws._start_channel_turn", fake_start
    )
    monkeypatch.setattr(bridge, "_session_for", lambda chat_id, reset: MagicMock())
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.apply_retention_inplace",
        lambda session, policy, **kwargs: None,
    )

    voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=4,
        ref="V1",
    )
    msg = TelegramMessage(
        update_id=1,
        message_id=7,
        chat_id=99,
        chat_type="private",
        from_user_id=11,
        from_username="jane.doe",
        text="",
        date=100,
        attachments=(voice,),
    )

    # Must NOT raise — the safety net keeps the poll loop alive.
    await bridge._handle_message(msg)

    body = captured["body"]
    assert 'status="extract_failed"' in body
    assert "internal decoder error" in body


@pytest.mark.asyncio
async def test_sibling_attachments_still_decode_when_one_decoder_crashes(
    tmp_path, monkeypatch
) -> None:
    """A decoder bug on attachment N must not poison attachment N+1."""
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def selective_decode(att, message=None):
        del message
        if att.kind == "voice":
            raise RuntimeError("voice decoder exploded")
        # photo / document / unknown — return unchanged so they keep
        # whatever status they came in with.
        return att

    bridge._decode_attachment = selective_decode

    captured: dict = {}

    async def fake_start(app, session, *, channel, chat_id, body, on_progress=None):
        del app, session, channel, chat_id
        captured["body"] = body
        return ""

    monkeypatch.setattr(
        "tesseract.mirror.server.ws._start_channel_turn", fake_start
    )
    monkeypatch.setattr(bridge, "_session_for", lambda chat_id, reset: MagicMock())
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.apply_retention_inplace",
        lambda session, policy, **kwargs: None,
    )

    voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=4,
        ref="V1",
    )
    sticker = ChannelAttachment(
        kind="sticker",
        status="no_handler",
        source="telegram",
        caption="emoji=😀",
        ref="S1",
    )
    msg = TelegramMessage(
        update_id=2,
        message_id=8,
        chat_id=99,
        chat_type="private",
        from_user_id=11,
        from_username="jane.doe",
        text="",
        date=100,
        attachments=(voice, sticker),
    )

    await bridge._handle_message(msg)

    body = captured["body"]
    # Voice envelope ended up extract_failed via the safety net.
    voice_block = body[body.index('kind="voice"'):]
    voice_block = voice_block[: voice_block.index("</channel_attachment>")]
    assert 'status="extract_failed"' in voice_block
    # Sticker still rode through untouched.
    sticker_block = body[body.index('kind="sticker"'):]
    sticker_block = sticker_block[: sticker_block.index("</channel_attachment>")]
    assert 'status="no_handler"' in sticker_block
