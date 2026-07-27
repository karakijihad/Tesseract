"""Session 2 (2026-05-16) — bridge outbound media methods.

1. ``send_voice(text=...)`` synthesises via the TTS engine, converts WAV
   to OGG via :func:`wav_bytes_to_ogg_opus`, posts via ``api.send_voice``,
   and persists OGG bytes + appends an outbound conversation row.
2. ``send_voice(audio_bytes=...)`` skips TTS and sends bytes as-is.
3. ``send_voice`` requires exactly one of text / audio_bytes.
4. ``send_photo`` resolves source_path, posts, and persists.
5. ``send_document`` reads from path, defaults filename, posts, persists.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations.telegram.bridge import TelegramBridge
from tesseract.integrations.telegram.state import StateBundle, save_allowlist


def _build_bridge(tmp_path, monkeypatch, *, tts_engine=None) -> TelegramBridge:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_voice = AsyncMock(return_value={"message_id": 101})
    bridge._api.send_photo = AsyncMock(return_value={"message_id": 102})
    bridge._api.send_document = AsyncMock(return_value={"message_id": 103})
    bridge._conversations = MagicMock()
    bridge._app = {
        "tts_engine": tts_engine,
        "voice_state": None,
    }
    state_dir = tmp_path / "telegram"
    state_dir.mkdir(parents=True, exist_ok=True)
    bridge._state = StateBundle(dir_path=state_dir)
    bridge._state.allowlist.chat_ids.add(99)
    save_allowlist(bridge._state.allowlist_path, bridge._state.allowlist)
    return bridge


def _saved_files(tmp_path: Path) -> list[Path]:
    root = tmp_path / "uploads" / "channels"
    if not root.exists():
        return []
    return [
        p for p in sorted(root.rglob("*"))
        if p.is_file() and "_index" not in p.parts
    ]


@pytest.mark.asyncio
async def test_send_voice_text_synthesises_converts_and_persists(
    tmp_path, monkeypatch
) -> None:
    tts = MagicMock()
    tts.synthesize = AsyncMock(return_value=(b"RIFFwavbytes", "piper"))
    bridge = _build_bridge(tmp_path, monkeypatch, tts_engine=tts)

    async def fake_encode(wav_bytes):
        assert wav_bytes == b"RIFFwavbytes"
        return b"OggSfakeopus"

    monkeypatch.setattr(
        "tesseract.voice.encode.wav_bytes_to_ogg_opus", fake_encode,
    )

    result = await bridge.send_voice(
        chat_ref="99", text="hello jane",
        caption="brief", reply_to_message_id=42,
    )
    assert result == {"message_id": 101}

    # TTS was called with the operator-set text.
    tts.synthesize.assert_awaited_once()
    args, _ = tts.synthesize.call_args
    assert args[0] == "hello jane"

    # API was called with the OGG bytes from the encoder.
    bridge._api.send_voice.assert_awaited_once()
    api_kwargs = bridge._api.send_voice.await_args.kwargs
    assert api_kwargs["chat_id"] == 99
    assert api_kwargs["ogg_opus_bytes"] == b"OggSfakeopus"
    assert api_kwargs["caption"] == "brief"
    assert api_kwargs["reply_to_message_id"] == 42

    # Bytes persisted to uploads/channels.
    files = _saved_files(tmp_path)
    assert len(files) == 1
    assert files[0].read_bytes() == b"OggSfakeopus"
    assert "/voice/" in str(files[0]).replace("\\", "/")

    # Outbound row appended.
    assert bridge._conversations.append.called
    appended = bridge._conversations.append.call_args
    channel_message = appended.args[2]
    assert channel_message.direction == "outbound"
    assert channel_message.body == "hello jane"


@pytest.mark.asyncio
async def test_send_voice_with_audio_bytes_skips_tts(
    tmp_path, monkeypatch
) -> None:
    tts = MagicMock()
    tts.synthesize = AsyncMock()
    bridge = _build_bridge(tmp_path, monkeypatch, tts_engine=tts)

    await bridge.send_voice(
        chat_ref="99", audio_bytes=b"OggSpre-rendered",
    )
    tts.synthesize.assert_not_awaited()
    bridge._api.send_voice.assert_awaited_once_with(
        chat_id=99, ogg_opus_bytes=b"OggSpre-rendered",
        caption=None, reply_to_message_id=None,
    )


@pytest.mark.asyncio
async def test_send_voice_requires_exactly_one_source(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch, tts_engine=MagicMock())
    with pytest.raises(ValueError, match="exactly one"):
        await bridge.send_voice(chat_ref="99")
    with pytest.raises(ValueError, match="exactly one"):
        await bridge.send_voice(
            chat_ref="99", text="hi", audio_bytes=b"both",
        )


@pytest.mark.asyncio
async def test_send_photo_from_path_persists_and_uploads(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    img_path = tmp_path / "kitten.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0jpegbytes")

    result = await bridge.send_photo(
        chat_ref="99", source_path=str(img_path), caption="meow",
    )
    assert result == {"message_id": 102}

    api_kwargs = bridge._api.send_photo.await_args.kwargs
    assert api_kwargs["image_bytes"] == b"\xff\xd8\xff\xe0jpegbytes"
    assert api_kwargs["caption"] == "meow"

    files = _saved_files(tmp_path)
    # tmp_path has the source kitten.jpg AND the persisted outbound copy
    # — filter out the source by location (saved files are under uploads/).
    persisted = [
        p for p in files if "uploads" in p.parts and "channels" in p.parts
    ]
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_send_document_defaults_filename_from_path(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)

    doc_path = tmp_path / "report.pdf"
    doc_path.write_bytes(b"%PDF-1.4 body")

    await bridge.send_document(chat_ref="99", source_path=str(doc_path))

    api_kwargs = bridge._api.send_document.await_args.kwargs
    assert api_kwargs["filename"] == "report.pdf"
    assert api_kwargs["mime_type"] == "application/pdf"
    assert api_kwargs["document_bytes"] == b"%PDF-1.4 body"


@pytest.mark.asyncio
async def test_send_document_rejects_missing_file(
    tmp_path, monkeypatch
) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)
    with pytest.raises(FileNotFoundError):
        await bridge.send_document(
            chat_ref="99", source_path=str(tmp_path / "nope.pdf"),
        )
