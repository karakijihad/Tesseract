"""Voice attachment handler — Whisper STT for arbitrary audio containers (CR-2A)."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path

from tesseract.voice.providers import local_whisper
from tesseract.voice.providers.local_whisper import LocalWhisperConfig

log = logging.getLogger(__name__)


class VoiceHandlerError(RuntimeError):
    """Raised when transcription fails; bridge maps to ``status="extract_failed"``."""


_MIME_SUFFIX: dict[str, str] = {
    "audio/ogg": ".ogg",
    "audio/ogg; codecs=opus": ".ogg",
    "audio/opus": ".opus",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/webm": ".webm",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
}


def _suffix_for(mime: str | None) -> str:
    if not mime:
        return ".bin"
    base = mime.split(";")[0].strip().lower()
    return _MIME_SUFFIX.get(base, ".bin")


async def transcribe_voice_audio(
    audio_bytes: bytes,
    *,
    cfg: LocalWhisperConfig,
    mime: str | None = None,
) -> str:
    """Transcribe ``audio_bytes`` via faster-whisper; raises :class:`VoiceHandlerError` on failure."""
    if not audio_bytes:
        return ""

    suffix = _suffix_for(mime)
    timeout_s = max(float(cfg.timeout_seconds), 5.0)

    def run() -> str:
        # Persist to a real path so faster-whisper can decode the
        # container via its bundled PyAV/ffmpeg backend. NamedTemporaryFile
        # with delete=False because Windows refuses to reopen an open
        # NamedTemporaryFile by path; we unlink in the finally block.
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(audio_bytes)
            tmp_path = tf.name
        try:
            started = time.perf_counter()
            model = local_whisper.get_model(cfg)
            segments, info = model.transcribe(
                tmp_path,
                language=cfg.language,
                beam_size=cfg.beam_size,
                vad_filter=True,
            )
            text = " ".join(
                seg.text.strip()
                for seg in segments
                if getattr(seg, "text", "").strip()
            ).strip()
            duration_s = float(getattr(info, "duration", 0.0) or 0.0)
            log.info(
                "channel voice transcribed mime=%s duration=%.2fs in %.2fs chars=%d",
                mime or "?",
                duration_s,
                time.perf_counter() - started,
                len(text),
            )
            return text
        finally:
            try:
                Path(tmp_path).unlink()
            except OSError:
                log.debug("voice handler: temp file %s already gone", tmp_path)

    try:
        return await asyncio.wait_for(asyncio.to_thread(run), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise VoiceHandlerError(
            f"voice transcription timed out after {timeout_s:.1f}s"
        ) from exc
    except Exception as exc:
        raise VoiceHandlerError(f"voice transcription failed: {exc}") from exc


__all__ = ["VoiceHandlerError", "transcribe_voice_audio"]
