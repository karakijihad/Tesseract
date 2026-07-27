"""Session 2 (2026-05-16) — WAV→OGG/Opus encoder.

1. Real PyAV roundtrip: silence WAV → valid OGG/Opus container.
2. Empty input raises ``AudioEncodeError``.
3. Garbage bytes raise ``AudioEncodeError`` (not a generic exception).
"""

from __future__ import annotations

import struct

import pytest

from tesseract.voice.encode import AudioEncodeError, wav_bytes_to_ogg_opus


def _silence_wav(seconds: float = 0.5, rate: int = 22_050) -> bytes:
    """Generate a minimal mono s16le WAV with silence."""
    n_samples = int(seconds * rate)
    pcm = bytes(2 * n_samples)
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + 2 * n_samples)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", 2 * n_samples)
    )
    return header + pcm


@pytest.mark.asyncio
async def test_wav_to_ogg_opus_produces_valid_ogg_container() -> None:
    wav = _silence_wav(seconds=0.5)
    ogg = await wav_bytes_to_ogg_opus(wav)
    # OGG containers start with the "OggS" magic per RFC 3533.
    assert ogg.startswith(b"OggS"), f"missing OGG magic: {ogg[:8]!r}"
    # Encoded Opus silence is far smaller than the s16le source.
    assert len(ogg) < len(wav)
    # libopus chunks should produce at least a header + one page.
    assert len(ogg) > 100


@pytest.mark.asyncio
async def test_empty_wav_raises_audio_encode_error() -> None:
    with pytest.raises(AudioEncodeError, match="empty"):
        await wav_bytes_to_ogg_opus(b"")


@pytest.mark.asyncio
async def test_garbage_bytes_raise_audio_encode_error() -> None:
    with pytest.raises(AudioEncodeError):
        await wav_bytes_to_ogg_opus(b"not a wav file at all")
