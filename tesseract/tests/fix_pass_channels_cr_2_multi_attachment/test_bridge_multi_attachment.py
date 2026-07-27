"""CR-2B golden flow: a single inbound Telegram message carries multiple
attachments (voice + document); both handlers fire concurrently in
declaration order and TARS sees a single body with both ``<extracted>``
sections.

This test goes through ``_handle_message`` end-to-end, stubbing only the
network-touching seams (fetch + handlers + chat turn). The point is to
verify the bridge does not bail on the first attachment and that the
decoded envelope ordering matches the inbound attachment order.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._channel_attachment import ChannelAttachment
from tesseract.integrations._channels_config import ChannelsConfig
from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.api import TelegramMessage
from tesseract.integrations.telegram.bridge import TelegramBridge
from tesseract.integrations.telegram.download import FetchedAttachment
from tesseract.integrations.telegram.state import StateBundle, save_allowlist
from tesseract.voice.providers.local_whisper import LocalWhisperConfig


def _local_cfg() -> LocalWhisperConfig:
    return LocalWhisperConfig(
        provider="local_whisper",
        model="tiny",
        device="cpu",
        compute_type="int8",
        language="en",
        beam_size=1,
        timeout_seconds=20.0,
        preload=False,
    )


def _build_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_chat_action = AsyncMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 5})
    bridge._api.edit_message_text = AsyncMock()
    bridge._conversations = MagicMock()
    stt_engine = MagicMock()
    stt_engine.local_config = _local_cfg()
    bridge._app = {
        "channels_config": ChannelsConfig(),
        "stt_engine": stt_engine,
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
async def test_voice_plus_document_each_renders_extracted_section(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    fetched_refs: list[str] = []

    async def fake_fetch(file_id, *, api, max_bytes):
        del api, max_bytes
        fetched_refs.append(file_id)
        return FetchedAttachment(data=b"payload-" + file_id.encode(), size=10)

    async def fake_transcribe(audio_bytes, *, cfg, mime):
        del cfg, mime
        return "summarize this pdf"

    async def fake_extract(data, *, mime, filename, max_chars):
        del mime, filename, max_chars
        return "Q1 report — revenue up 14%."

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.transcribe_voice_audio",
        fake_transcribe,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.extract_document_text",
        fake_extract,
    )

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
    pdf = ChannelAttachment(
        kind="document",
        status="no_handler",
        source="telegram",
        mime="application/pdf",
        filename="report.pdf",
        ref="D1",
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
        attachments=(voice, pdf),
    )

    await bridge._handle_message(msg)

    body = captured["body"]
    assert body.count('status="ready"') == 2
    assert "summarize this pdf" in body
    assert "Q1 report" in body
    voice_idx = body.index('kind="voice"')
    doc_idx = body.index('kind="document"')
    assert voice_idx < doc_idx  # declaration order preserved
    # Both refs were fetched, in attachment order.
    assert fetched_refs == ["V1", "D1"]


@pytest.mark.asyncio
async def test_mixed_success_and_failure_does_not_block_each_other(
    tmp_path, monkeypatch
) -> None:
    """One attachment is too_large, the next still gets decoded — the
    bridge must not short-circuit on the first failure."""
    bridge = _build_bridge(tmp_path, monkeypatch)

    fetched_refs: list[str] = []

    async def fake_fetch(file_id, *, api, max_bytes):
        del api, max_bytes
        fetched_refs.append(file_id)
        return FetchedAttachment(data=b"payload", size=7)

    async def fake_extract(data, *, mime, filename, max_chars):
        del data, mime, filename, max_chars
        return "doc body"

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.extract_document_text",
        fake_extract,
    )

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

    # voice over duration cap (cap is 600s).
    over_voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=900,
        ref="VLong",
    )
    doc = ChannelAttachment(
        kind="document",
        status="no_handler",
        source="telegram",
        mime="application/pdf",
        filename="ok.pdf",
        ref="D1",
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
        attachments=(over_voice, doc),
    )

    await bridge._handle_message(msg)

    body = captured["body"]
    assert 'status="too_large"' in body
    assert 'status="ready"' in body
    assert "doc body" in body
    # voice was skipped before fetch; only doc was fetched.
    assert fetched_refs == ["D1"]
