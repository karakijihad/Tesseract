"""CR-2A unit tests: :mod:`tesseract.integrations._handlers.voice`.

The Telegram bridge writes attachment bytes to a temp file and lets
faster-whisper decode the container; we mock the model handle so the
tests don't load real Whisper weights but still exercise the temp-file
path, mime → suffix mapping, timeout handling, and error wrapping.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tesseract.integrations._handlers.voice import (
    VoiceHandlerError,
    _suffix_for,
    transcribe_voice_audio,
)
from tesseract.voice.providers.local_whisper import LocalWhisperConfig


def _cfg(timeout: float = 20.0) -> LocalWhisperConfig:
    return LocalWhisperConfig(
        provider="local_whisper",
        model="tiny",
        device="cpu",
        compute_type="int8",
        language="en",
        beam_size=1,
        timeout_seconds=timeout,
        preload=False,
    )


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeInfo:
    def __init__(self, duration: float) -> None:
        self.duration = duration


class _FakeModel:
    def __init__(
        self,
        text: str = "hello world",
        duration: float = 1.0,
        *,
        record_paths: list[Path] | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._text = text
        self._duration = duration
        self._record_paths = record_paths
        self._raise = raise_exc

    def transcribe(self, audio, *, language, beam_size, vad_filter):
        del language, beam_size, vad_filter
        if self._record_paths is not None:
            self._record_paths.append(Path(audio))
        if self._raise is not None:
            raise self._raise
        return ([_FakeSegment(self._text)], _FakeInfo(self._duration))


@pytest.mark.parametrize(
    "mime,suffix",
    [
        ("audio/ogg", ".ogg"),
        ("audio/ogg; codecs=opus", ".ogg"),
        ("audio/mp4", ".m4a"),
        ("audio/x-m4a", ".m4a"),
        ("audio/mpeg", ".mp3"),
        ("audio/wav", ".wav"),
        ("audio/webm", ".webm"),
        ("audio/flac", ".flac"),
        (None, ".bin"),
        ("application/octet-stream", ".bin"),
    ],
)
def test_suffix_for_known_mimes(mime, suffix) -> None:
    assert _suffix_for(mime) == suffix


@pytest.mark.asyncio
async def test_empty_bytes_returns_empty_without_calling_model(monkeypatch) -> None:
    called = False

    def fake_get_model(cfg):
        nonlocal called
        called = True
        return _FakeModel()

    monkeypatch.setattr(
        "tesseract.integrations._handlers.voice.local_whisper.get_model",
        fake_get_model,
    )

    out = await transcribe_voice_audio(b"", cfg=_cfg(), mime="audio/ogg")
    assert out == ""
    assert called is False


@pytest.mark.asyncio
async def test_transcribes_via_temp_file_and_returns_stripped_text(
    monkeypatch, tmp_path
) -> None:
    captured: list[Path] = []
    fake = _FakeModel(text="  hello world  ", record_paths=captured)
    monkeypatch.setattr(
        "tesseract.integrations._handlers.voice.local_whisper.get_model",
        lambda cfg: fake,
    )

    out = await transcribe_voice_audio(b"OggS\x00fake", cfg=_cfg(), mime="audio/ogg")
    assert out == "hello world"
    # Temp file was created with the .ogg suffix from the mime mapping
    # and cleaned up before this assertion runs.
    assert len(captured) == 1
    assert captured[0].suffix == ".ogg"
    assert not captured[0].exists()


@pytest.mark.asyncio
async def test_multiple_segments_joined_with_single_space(monkeypatch) -> None:
    class _MultiSegmentModel:
        def transcribe(self, audio, *, language, beam_size, vad_filter):
            del audio, language, beam_size, vad_filter
            return (
                [_FakeSegment("foo"), _FakeSegment(""), _FakeSegment("  bar  ")],
                _FakeInfo(2.0),
            )

    monkeypatch.setattr(
        "tesseract.integrations._handlers.voice.local_whisper.get_model",
        lambda cfg: _MultiSegmentModel(),
    )

    out = await transcribe_voice_audio(
        b"OggS\x00fake", cfg=_cfg(), mime="audio/ogg"
    )
    assert out == "foo bar"


@pytest.mark.asyncio
async def test_model_exception_wrapped_in_voice_handler_error(monkeypatch) -> None:
    fake = _FakeModel(raise_exc=RuntimeError("CUDA out of memory"))
    monkeypatch.setattr(
        "tesseract.integrations._handlers.voice.local_whisper.get_model",
        lambda cfg: fake,
    )

    with pytest.raises(VoiceHandlerError) as exc_info:
        await transcribe_voice_audio(
            b"OggS\x00fake", cfg=_cfg(), mime="audio/ogg"
        )
    assert "CUDA out of memory" in str(exc_info.value)


@pytest.mark.asyncio
async def test_timeout_surfaces_as_voice_handler_error(monkeypatch) -> None:
    async def slow_to_thread(fn, *args, **kwargs):
        del fn, args, kwargs
        await asyncio.sleep(10.0)
        return ""

    monkeypatch.setattr(asyncio, "to_thread", slow_to_thread)

    with pytest.raises(VoiceHandlerError) as exc_info:
        await transcribe_voice_audio(
            b"OggS\x00fake", cfg=_cfg(timeout=0.05), mime="audio/ogg"
        )
    assert "timed out" in str(exc_info.value)


@pytest.mark.asyncio
async def test_temp_file_cleaned_up_even_on_model_error(monkeypatch) -> None:
    captured: list[Path] = []
    fake = _FakeModel(record_paths=captured, raise_exc=RuntimeError("decode error"))
    monkeypatch.setattr(
        "tesseract.integrations._handlers.voice.local_whisper.get_model",
        lambda cfg: fake,
    )

    with pytest.raises(VoiceHandlerError):
        await transcribe_voice_audio(
            b"OggS\x00fake", cfg=_cfg(), mime="audio/ogg"
        )
    assert len(captured) == 1
    assert not captured[0].exists()
