"""CR-2A integration: the Telegram bridge calls the voice handler and
promotes the envelope from ``no_handler`` to ``ready``.

This test crosses the bridge boundary but stubs the actual STT call —
real Whisper would require model weights and PyAV decoding. The contract
covered:

1. A no_handler voice attachment runs through ``_decode_voice``.
2. ``fetch_telegram_attachment`` is invoked with the file_id from the
   attachment's ``ref`` field.
3. The resulting envelope reaches ``_start_channel_turn`` with
   ``status="ready"`` and an ``<extracted>`` body holding the transcript.
4. Telegram ``too_large`` is honored against ``channels.yaml`` caps before
   any network call happens.
5. Decoder failures collapse cleanly to ``extract_failed`` so the
   envelope still reaches TARS.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._channel_attachment import ChannelAttachment
from tesseract.integrations._channels_config import ChannelsConfig
from tesseract.integrations._handlers.voice import VoiceHandlerError
from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.api import TelegramMessage
from tesseract.integrations.telegram.bridge import TelegramBridge
from tesseract.integrations.telegram.download import (
    FetchRejection,
    FetchedAttachment,
)
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


def _build_bridge(tmp_path, monkeypatch, *, stt_engine=None, channels_cfg=None):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_chat_action = AsyncMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 5})
    bridge._api.edit_message_text = AsyncMock()
    bridge._conversations = MagicMock()
    bridge._app = {
        "stt_engine": stt_engine,
        "channels_config": channels_cfg or ChannelsConfig(),
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


def _stt_engine_with_local() -> MagicMock:
    engine = MagicMock()
    engine.local_config = _local_cfg()
    return engine


def _build_message(att: ChannelAttachment, *, text: str = "") -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=7,
        chat_id=99,
        chat_type="private",
        from_user_id=11,
        from_username="jane.doe",
        text=text,
        date=100,
        attachments=(att,),
    )


def _stub_turn(monkeypatch, capture: dict) -> None:
    async def fake_start(app, session, *, channel, chat_id, body, on_progress=None):
        del app, session, channel, chat_id
        capture["body"] = body
        return ""

    monkeypatch.setattr(
        "tesseract.mirror.server.ws._start_channel_turn", fake_start
    )


def _stub_session_and_retention(monkeypatch, bridge) -> None:
    monkeypatch.setattr(bridge, "_session_for", lambda chat_id, reset: MagicMock())
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.apply_retention_inplace",
        lambda session, policy, **kwargs: None,
    )


@pytest.mark.asyncio
async def test_voice_no_handler_becomes_ready_with_transcript(
    tmp_path, monkeypatch
) -> None:
    engine = _stt_engine_with_local()
    bridge = _build_bridge(tmp_path, monkeypatch, stt_engine=engine)

    async def fake_fetch(file_id, *, api, max_bytes):
        del api, max_bytes
        assert file_id == "AwACA"
        return FetchedAttachment(data=b"OggS\x00audio", size=10)

    async def fake_transcribe(audio_bytes, *, cfg, mime):
        del audio_bytes, cfg
        assert mime == "audio/ogg"
        return "hello tars"

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.transcribe_voice_audio",
        fake_transcribe,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=3,
        ref="AwACA",
    )
    await bridge._handle_message(_build_message(voice))

    body = captured["body"]
    assert 'status="ready"' in body
    assert "<extracted>" in body
    assert "hello tars" in body
    # Cleanup tells: original no_handler is gone.
    assert 'status="no_handler"' not in body


@pytest.mark.asyncio
async def test_voice_over_duration_cap_skips_fetch_and_marks_too_large(
    tmp_path, monkeypatch
) -> None:
    engine = _stt_engine_with_local()
    bridge = _build_bridge(tmp_path, monkeypatch, stt_engine=engine)

    fetch_calls: list = []

    async def fake_fetch(file_id, *, api, max_bytes):
        fetch_calls.append(file_id)
        return FetchedAttachment(data=b"", size=0)

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    # voice cap is 600s by default; this voice claims 700s.
    voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=700,
        ref="AwACA",
    )
    await bridge._handle_message(_build_message(voice))

    body = captured["body"]
    assert 'status="too_large"' in body
    assert "exceeds 600s cap" in body
    assert fetch_calls == []


@pytest.mark.asyncio
async def test_voice_fetch_rejection_collapses_to_extract_failed(
    tmp_path, monkeypatch
) -> None:
    engine = _stt_engine_with_local()
    bridge = _build_bridge(tmp_path, monkeypatch, stt_engine=engine)

    async def fake_fetch(file_id, *, api, max_bytes):
        del file_id, api, max_bytes
        return FetchRejection(kind="fetch_failed", detail="HTTP 502 from CDN")

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=3,
        ref="AwACA",
    )
    await bridge._handle_message(_build_message(voice))

    body = captured["body"]
    assert 'status="extract_failed"' in body
    assert "HTTP 502" in body


@pytest.mark.asyncio
async def test_voice_handler_error_becomes_extract_failed(
    tmp_path, monkeypatch
) -> None:
    engine = _stt_engine_with_local()
    bridge = _build_bridge(tmp_path, monkeypatch, stt_engine=engine)

    async def fake_fetch(file_id, *, api, max_bytes):
        del file_id, api, max_bytes
        return FetchedAttachment(data=b"OggS\x00audio", size=10)

    async def fake_transcribe(audio_bytes, *, cfg, mime):
        del audio_bytes, cfg, mime
        raise VoiceHandlerError("Whisper load failed")

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.transcribe_voice_audio",
        fake_transcribe,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=3,
        ref="AwACA",
    )
    await bridge._handle_message(_build_message(voice))

    body = captured["body"]
    assert 'status="extract_failed"' in body
    assert "Whisper load failed" in body


@pytest.mark.asyncio
async def test_voice_without_stt_engine_marks_extract_failed(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch, stt_engine=None)

    fetch_calls: list = []

    async def fake_fetch(file_id, *, api, max_bytes):
        fetch_calls.append(file_id)
        return FetchedAttachment(data=b"OggS\x00audio", size=10)

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=3,
        ref="AwACA",
    )
    await bridge._handle_message(_build_message(voice))

    body = captured["body"]
    assert 'status="extract_failed"' in body
    assert "Whisper" in body
    assert fetch_calls == []  # short-circuit before fetch


@pytest.mark.asyncio
async def test_empty_transcript_marks_extract_failed(
    tmp_path, monkeypatch
) -> None:
    engine = _stt_engine_with_local()
    bridge = _build_bridge(tmp_path, monkeypatch, stt_engine=engine)

    async def fake_fetch(file_id, *, api, max_bytes):
        del file_id, api, max_bytes
        return FetchedAttachment(data=b"OggS\x00audio", size=10)

    async def fake_transcribe(audio_bytes, *, cfg, mime):
        del audio_bytes, cfg, mime
        return "   "

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.transcribe_voice_audio",
        fake_transcribe,
    )

    captured: dict = {}
    _stub_turn(monkeypatch, captured)
    _stub_session_and_retention(monkeypatch, bridge)

    voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=3,
        ref="AwACA",
    )
    await bridge._handle_message(_build_message(voice))

    body = captured["body"]
    assert 'status="extract_failed"' in body
    assert "transcript empty" in body
