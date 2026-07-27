"""Audio format converters for outbound channel media.

Local TTS (Piper / Kokoro) emits WAV. Telegram's ``sendVoice`` endpoint
only renders the voice-note UI (round play button, waveform) for
``.ogg`` files encoded with the Opus codec — sending WAV through that
endpoint produces a generic "audio file" pill, which kills the
"feels like a real person sent a voice message" cue.

This module wraps PyAV (already in the dependency tree via
``faster-whisper``) to transcode WAV → OGG/Opus in-process. No subprocess
ffmpeg call; no temp files. PyAV's ``libopus`` codec ships with the wheel
on Windows/Linux/macOS — :func:`wav_bytes_to_ogg_opus` raises a clear
:class:`AudioEncodeError` if the codec is missing so the operator knows
exactly which dependency to fix.
"""

from __future__ import annotations

import asyncio
import io
import logging

log = logging.getLogger(__name__)


class AudioEncodeError(RuntimeError):
    """Raised when WAV→OGG/Opus conversion fails."""


_TELEGRAM_OPUS_BITRATE = 32_000  # 32 kbps — Telegram's voice-note default
_TELEGRAM_OPUS_SAMPLE_RATE = 48_000  # Opus is canonically 48 kHz


def _convert_sync(wav_bytes: bytes) -> bytes:
    """Synchronous PyAV transcode; run via :func:`wav_bytes_to_ogg_opus`.

    Reads the input as a generic container so the WAV header is auto-
    detected (PyAV demuxes via libavformat). Resamples to 48 kHz mono
    (Telegram voice notes are always mono) before encoding to libopus
    inside an OGG container. Returns the OGG bytes ready to POST to
    ``sendVoice``.
    """
    try:
        import av  # type: ignore
        from av.audio.resampler import AudioResampler  # type: ignore
    except ImportError as exc:
        raise AudioEncodeError(
            "PyAV is required for WAV→OGG/Opus conversion. "
            "Install with `pip install av>=11.0`."
        ) from exc

    if not wav_bytes:
        raise AudioEncodeError("WAV bytes are empty")

    input_buf = io.BytesIO(wav_bytes)
    try:
        in_container = av.open(input_buf, mode="r")
    except av.FFmpegError as exc:  # type: ignore[attr-defined]
        raise AudioEncodeError(f"failed to open WAV input: {exc}") from exc

    output_buf = io.BytesIO()
    try:
        out_container = av.open(output_buf, mode="w", format="ogg")
    except av.FFmpegError as exc:  # type: ignore[attr-defined]
        raise AudioEncodeError(f"failed to open OGG output: {exc}") from exc

    try:
        try:
            audio_stream = out_container.add_stream("libopus", rate=_TELEGRAM_OPUS_SAMPLE_RATE)
        except Exception as exc:
            raise AudioEncodeError(
                "libopus codec unavailable in PyAV build — "
                "reinstall PyAV with opus support."
            ) from exc

        audio_stream.bit_rate = _TELEGRAM_OPUS_BITRATE
        audio_stream.layout = "mono"

        resampler = AudioResampler(
            format="s16",
            layout="mono",
            rate=_TELEGRAM_OPUS_SAMPLE_RATE,
        )

        in_audio = next(
            (s for s in in_container.streams if s.type == "audio"), None,
        )
        if in_audio is None:
            raise AudioEncodeError("WAV input has no audio stream")

        try:
            for frame in in_container.decode(in_audio):
                for resampled in resampler.resample(frame):
                    for packet in audio_stream.encode(resampled):
                        out_container.mux(packet)

            # Flush both resampler and encoder.
            for resampled in resampler.resample(None) or []:
                for packet in audio_stream.encode(resampled):
                    out_container.mux(packet)
            for packet in audio_stream.encode(None):
                out_container.mux(packet)
        except av.FFmpegError as exc:  # type: ignore[attr-defined]
            # Mid-stream decode or encode failure — wrap so the operator
            # sees the friendly AudioEncodeError instead of a raw PyAV
            # exception leaking through the bridge.
            raise AudioEncodeError(f"transcode failed mid-stream: {exc}") from exc
    finally:
        try:
            out_container.close()
        except Exception:
            log.debug("encode: output close raised", exc_info=True)
        try:
            in_container.close()
        except Exception:
            log.debug("encode: input close raised", exc_info=True)

    return output_buf.getvalue()


async def wav_bytes_to_ogg_opus(wav_bytes: bytes) -> bytes:
    """Async wrapper — runs PyAV on the default executor so the event
    loop never blocks on libavcodec. Returns OGG/Opus bytes suitable for
    Telegram ``sendVoice``."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _convert_sync, wav_bytes)


__all__ = ["wav_bytes_to_ogg_opus", "AudioEncodeError"]
