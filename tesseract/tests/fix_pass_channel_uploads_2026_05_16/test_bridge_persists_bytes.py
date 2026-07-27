"""Session 1 (2026-05-16) — bridge persists inbound bytes alongside extraction.

Asserts that:
1. ``_decode_voice`` stamps ``storage_path`` onto the returned attachment
   after a successful fetch, and the bytes land at the expected layout.
2. ``_decode_photo`` same.
3. ``_decode_document`` same.
4. Undecoded kinds (video/animation/audio/sticker/video_note) flow through
   ``_persist_undecoded`` — bytes saved, status stays ``no_handler`` but
   ``storage_path`` is set so TARS can reference the saved file later.
5. Persistence failure is non-fatal — the attachment still gets through
   with whatever extraction worked, just without ``storage_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
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


def _build_bridge(tmp_path, monkeypatch, *, stt_engine=None) -> TelegramBridge:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_chat_action = AsyncMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 5})
    bridge._api.edit_message_text = AsyncMock()
    bridge._conversations = MagicMock()
    bridge._app = {
        "stt_engine": stt_engine,
        "channels_config": ChannelsConfig(),
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


def _msg(att: ChannelAttachment, *, message_id: int = 7) -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=message_id,
        chat_id=99,
        chat_type="private",
        from_user_id=11,
        from_username="jane.doe",
        text="",
        date=100,
        attachments=(att,),
    )


def _saved_files_under(tmp_path: Path) -> list[Path]:
    """All files written under uploads/channels/, excluding the JSON index."""
    root = tmp_path / "uploads" / "channels"
    if not root.exists():
        return []
    return [
        p for p in sorted(root.rglob("*"))
        if p.is_file() and "_index" not in p.parts
    ]


def _index_entries(tmp_path: Path, channel: str, chat_id: str) -> list[dict]:
    path = tmp_path / "uploads" / "channels" / "_index" / channel / f"{chat_id}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_decode_voice_persists_bytes_and_stamps_storage_path(
    tmp_path, monkeypatch
) -> None:
    engine = MagicMock()
    engine.local_config = _local_cfg()
    bridge = _build_bridge(tmp_path, monkeypatch, stt_engine=engine)

    async def fake_fetch(file_id, *, api, max_bytes):
        del api, max_bytes
        assert file_id == "VOICE-REF"
        return FetchedAttachment(data=b"OggS\x00voice-bytes", size=15)

    async def fake_transcribe(audio_bytes, *, cfg, mime):
        del audio_bytes, cfg, mime
        return "hello tars"

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.transcribe_voice_audio",
        fake_transcribe,
    )

    voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=3,
        ref="VOICE-REF",
    )
    result = await bridge._decode_voice(voice, _msg(voice, message_id=42))

    assert result.status == "ready"
    assert result.extracted == "hello tars"
    assert result.storage_path is not None
    assert result.storage_path.startswith("telegram/99/")
    assert "/42/" in result.storage_path  # message_id appears in path
    assert "/voice/" in result.storage_path  # bucket for kind=voice

    files = _saved_files_under(tmp_path)
    assert len(files) == 1
    assert files[0].read_bytes() == b"OggS\x00voice-bytes"

    entries = _index_entries(tmp_path, "telegram", "99")
    assert len(entries) == 1
    assert entries[0]["source_ref"] == "VOICE-REF"


@pytest.mark.asyncio
async def test_decode_photo_persists_bytes_and_stamps_storage_path(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def fake_fetch(file_id, *, api, max_bytes):
        del file_id, api, max_bytes
        return FetchedAttachment(data=b"\xff\xd8\xff\xe0jpegbytes", size=15)

    async def fake_describe(data, *, mime, caption, cost_ledger, max_chars):
        del data, mime, caption, cost_ledger, max_chars
        return "a cat sitting on a keyboard"

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.describe_image",
        fake_describe,
    )

    photo = ChannelAttachment(
        kind="photo",
        status="no_handler",
        source="telegram",
        mime="image/jpeg",
        size=15,
        ref="PHOTO-REF",
    )
    result = await bridge._decode_photo(photo, _msg(photo, message_id=43))

    assert result.status == "ready"
    assert result.extracted == "a cat sitting on a keyboard"
    assert result.storage_path is not None
    assert "/image/" in result.storage_path  # bucket for kind=photo
    files = _saved_files_under(tmp_path)
    assert len(files) == 1
    assert files[0].read_bytes() == b"\xff\xd8\xff\xe0jpegbytes"


@pytest.mark.asyncio
async def test_decode_document_persists_bytes_and_stamps_storage_path(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def fake_fetch(file_id, *, api, max_bytes):
        del file_id, api, max_bytes
        return FetchedAttachment(data=b"%PDF-1.4 doc bytes", size=18)

    async def fake_extract(data, *, mime, filename, max_chars):
        del data, mime, filename, max_chars
        return "Document body text"

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.extract_document_text",
        fake_extract,
    )

    doc = ChannelAttachment(
        kind="document",
        status="no_handler",
        source="telegram",
        mime="application/pdf",
        size=18,
        filename="report.pdf",
        ref="DOC-REF",
    )
    result = await bridge._decode_document(doc, _msg(doc, message_id=44))

    assert result.status == "ready"
    assert result.extracted == "Document body text"
    assert result.storage_path is not None
    assert "/document/" in result.storage_path
    assert result.storage_path.endswith("report.pdf")


@pytest.mark.asyncio
async def test_undecoded_kinds_are_persisted_with_storage_path(
    tmp_path, monkeypatch
) -> None:
    """video/animation/audio/sticker/video_note carry no extractor but
    must still be saved so TARS can reference them later."""
    bridge = _build_bridge(tmp_path, monkeypatch)

    async def fake_fetch(file_id, *, api, max_bytes):
        del file_id, api, max_bytes
        return FetchedAttachment(data=b"animation-gif-bytes", size=19)

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )

    anim = ChannelAttachment(
        kind="animation",
        status="no_handler",
        source="telegram",
        mime="video/mp4",
        size=19,
        ref="ANIM-REF",
    )
    result = await bridge._decode_attachment(anim, _msg(anim, message_id=51))

    # Status stays no_handler (we never extracted text), but storage_path
    # is set so TARS can talk about "the GIF you just sent".
    assert result.status == "no_handler"
    assert result.storage_path is not None
    assert "/video/" in result.storage_path  # bucket for kind=animation
    files = _saved_files_under(tmp_path)
    assert len(files) == 1


@pytest.mark.asyncio
async def test_persist_failure_is_non_fatal(tmp_path, monkeypatch) -> None:
    """A wedged disk must not block the transcript reaching TARS."""
    engine = MagicMock()
    engine.local_config = _local_cfg()
    bridge = _build_bridge(tmp_path, monkeypatch, stt_engine=engine)

    async def fake_fetch(file_id, *, api, max_bytes):
        del file_id, api, max_bytes
        return FetchedAttachment(data=b"voice", size=5)

    async def fake_transcribe(audio_bytes, *, cfg, mime):
        del audio_bytes, cfg, mime
        return "transcript despite disk fail"

    async def broken_save(**kwargs):
        del kwargs
        raise OSError("disk wedged")

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.fetch_telegram_attachment",
        fake_fetch,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.transcribe_voice_audio",
        fake_transcribe,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.save_channel_attachment",
        broken_save,
    )

    voice = ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime="audio/ogg",
        duration_s=2,
        ref="VOICE-REF",
    )
    result = await bridge._decode_voice(voice, _msg(voice))

    assert result.status == "ready"
    assert result.extracted == "transcript despite disk fail"
    # Persistence failed, so no storage_path — but the turn still carries
    # the transcript.
    assert result.storage_path is None
