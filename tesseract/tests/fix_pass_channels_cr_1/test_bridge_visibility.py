"""CR-1: the Telegram bridge feeds the ``<channel_attachment>`` envelope
to ``_start_channel_turn`` and to the conversation log.

We don't boot a real Telegram poll loop — instead we mount the bridge's
``_handle_message`` directly with a fixture-shaped :class:`TelegramMessage`
and patch the WS-bound dependencies.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._channel_attachment import ChannelAttachment
from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.api import TelegramMessage
from tesseract.integrations.telegram.bridge import TelegramBridge
from tesseract.integrations.telegram.state import StateBundle, save_allowlist


async def _identity_decode(attachments, message=None):
    """CR-1 tests assert on ``no_handler`` envelope output. CR-2 added a
    decoder dispatch in :meth:`TelegramBridge._handle_message`; bypassing
    it here keeps these tests focused on CR-1 wiring without doubling up
    on CR-2's decoder coverage (which lives under
    ``fix_pass_channels_cr_2_voice`` / ``..._image``).

    The ``message`` param is the Session-1 (2026-05-16) addition that
    threads ``TelegramMessage`` into decoders so persistence can stamp
    chat/message ids onto the saved bytes; ignored here."""
    del message
    return attachments


def _build_bridge(tmp_path, monkeypatch) -> TelegramBridge:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_chat_action = AsyncMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 5})
    bridge._api.edit_message_text = AsyncMock()
    bridge._conversations = MagicMock()
    bridge._app = MagicMock()
    bridge._sessions = {}
    bridge._retention = RetentionPolicy.fallback()
    bridge._poll_task = None
    bridge._stop_event = MagicMock()
    bridge._started_at = ""
    bridge._last_poll_at = None
    bridge._error_count = 0
    bridge._decode_attachments = _identity_decode

    state_dir = tmp_path / "telegram"
    state_dir.mkdir(parents=True, exist_ok=True)
    bridge._state = StateBundle(dir_path=state_dir)
    # Whitelist the test chat_id so the bridge does not park it in
    # ``pending`` and short-circuit before envelope construction.
    # ``_handle_message`` reloads the allowlist from disk on entry, so
    # the add must be persisted before the call.
    bridge._state.allowlist.chat_ids.add(99)
    save_allowlist(bridge._state.allowlist_path, bridge._state.allowlist)
    return bridge


@pytest.mark.asyncio
async def test_voice_message_lands_envelope_in_chat_turn_body(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    captured = {}

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
        duration_s=12,
        ref="AwACA",
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

    await bridge._handle_message(msg)

    body = captured["body"]
    assert body.startswith('<channel_attachment kind="voice"')
    assert 'status="no_handler"' in body
    assert 'source="telegram"' in body
    assert 'ref="AwACA"' in body


@pytest.mark.asyncio
async def test_text_plus_photo_concatenates_text_then_envelope(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    captured = {}

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

    photo = ChannelAttachment(
        kind="photo",
        status="no_handler",
        source="telegram",
        width=800,
        height=600,
        caption="check this",
        ref="P1",
    )
    msg = TelegramMessage(
        update_id=2,
        message_id=8,
        chat_id=99,
        chat_type="private",
        from_user_id=11,
        from_username="jane.doe",
        text="look",
        date=100,
        attachments=(photo,),
    )

    await bridge._handle_message(msg)

    body = captured["body"]
    assert body.startswith("look\n\n<channel_attachment")
    assert 'kind="photo"' in body


@pytest.mark.asyncio
async def test_conversation_log_records_rendered_envelope(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def fake_start(app, session, *, channel, chat_id, body, on_progress=None):
        del app, session, channel, chat_id, body
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
        kind="voice", status="no_handler", source="telegram", duration_s=3
    )
    msg = TelegramMessage(
        update_id=3,
        message_id=9,
        chat_id=99,
        chat_type="private",
        from_user_id=11,
        from_username="jane.doe",
        text="",
        date=100,
        attachments=(voice,),
    )

    await bridge._handle_message(msg)

    appended = bridge._conversations.append.call_args
    assert appended is not None
    args, _ = appended
    channel_message = args[2]
    assert channel_message.direction == "inbound"
    assert "<channel_attachment kind=\"voice\"" in channel_message.body
    assert channel_message.extra.get("has_attachments") is True


@pytest.mark.asyncio
async def test_text_only_omits_envelope(tmp_path, monkeypatch) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    captured = {}

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

    msg = TelegramMessage(
        update_id=4,
        message_id=10,
        chat_id=99,
        chat_type="private",
        from_user_id=11,
        from_username="jane.doe",
        text="plain text",
        date=100,
        attachments=(),
    )

    await bridge._handle_message(msg)

    assert captured["body"] == "plain text"
