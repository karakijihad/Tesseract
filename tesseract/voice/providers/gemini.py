"""Gemini Flash audio transcription via google-genai.

The provider wraps `client.aio.models.generate_content(...)` with an
audio inline_data part. Mirror sends raw 16-bit/16 kHz mono PCM frames;
we wrap them in a WAV envelope here so Gemini's audio decoder accepts
the input — the model rejects headerless PCM with a 400.

The `google-genai` Client carries a small lifetime cost (transport +
auth init) so we cache one per process keyed on `(api_key, ...)`. Tests
inject a fake client via `set_client_factory()` to avoid hitting the
network.

Cost: Gemini 2.5 Flash audio input is billed at ~32 tokens/sec at
$1/Mtok (≈ $0.115/audio-hour). Output text is short (transcript only)
and contributes negligibly. The ledger debits per audio-second on the
caller side; this module records nothing itself.
"""

from __future__ import annotations

import io
import logging
import os
import wave
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_PCM_SAMPLE_RATE_HZ = 16_000
_PCM_BITS_PER_SAMPLE = 16
_PCM_CHANNELS = 1


@dataclass(frozen=True)
class GeminiSTTConfig:
    model: str          # e.g. "gemini-2.5-flash"
    api_key_env: str    # e.g. "GOOGLE_API_KEY"
    prompt: str         # short instruction prefacing the audio part
    timeout_seconds: float


class GeminiSTTError(RuntimeError):
    """Wraps any failure path so the engine surfaces a single error type."""


_ClientFactory = Callable[[str], Any]
_GenerateFn = Callable[..., Awaitable[Any]]

_client_cache: dict[str, Any] = {}
_client_factory: _ClientFactory | None = None


def set_client_factory(factory: _ClientFactory | None) -> None:
    """Override the default `genai.Client(api_key=...)` constructor.

    Tests pass a stub that returns an object with `.aio.models.generate_content`.
    Pass `None` to restore the real path."""
    global _client_factory
    _client_factory = factory
    _client_cache.clear()


def _default_factory(api_key: str) -> Any:
    try:
        from google import genai  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - hard dep in pyproject
        raise GeminiSTTError(
            "google-genai not installed — `pip install google-genai`"
        ) from exc
    return genai.Client(api_key=api_key)


def _get_client(api_key_env: str, api_key: str) -> Any:
    """Cache one client per env-var slot (not per resolved key value), so
    a rotated `GOOGLE_API_KEY` evicts the stale client on the next call
    when paired with `set_client_factory(None)` — which clears the cache.
    Keying by env-var name avoids unbounded cache growth across rotations."""
    cached = _client_cache.get(api_key_env)
    if cached is not None:
        return cached
    factory = _client_factory or _default_factory
    client = factory(api_key)
    _client_cache[api_key_env] = client
    return client


def _is_wav(audio: bytes) -> bool:
    return len(audio) >= 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"


def _wrap_pcm_in_wav(pcm: bytes) -> bytes:
    """Wrap raw 16-bit mono 16 kHz PCM in a WAV container.

    Mirror's `AudioCapture` worklet emits headerless little-endian Int16
    at 16 kHz; Gemini's `audio/wav` decoder needs a real RIFF envelope.
    Stdlib `wave` writes the 44-byte header correctly."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(_PCM_CHANNELS)
        w.setsampwidth(_PCM_BITS_PER_SAMPLE // 8)
        w.setframerate(_PCM_SAMPLE_RATE_HZ)
        w.writeframes(pcm)
    return buf.getvalue()


def _audio_part(audio: bytes) -> Any:
    """Build the `types.Part` carrying the audio bytes. The mime type
    must match the container — WAV for our wrapped PCM."""
    try:
        from google.genai import types  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - hard dep in pyproject
        raise GeminiSTTError("google-genai missing types module") from exc
    return types.Part.from_bytes(data=audio, mime_type="audio/wav")


async def transcribe(audio_bytes: bytes, cfg: GeminiSTTConfig) -> str:
    """Transcribe `audio_bytes` via Gemini Flash audio. Returns the
    stripped transcript text (or empty string on whitespace-only output).

    The caller is expected to have already debited the cost ledger; this
    provider stays pure-IO so `STTEngine` owns the metering invariant.
    Raises `GeminiSTTError` on any transport / API / decode failure."""
    if not audio_bytes:
        return ""

    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        raise GeminiSTTError(
            f"{cfg.api_key_env} not set in environment (add to tesseract/.env)"
        )

    payload = audio_bytes if _is_wav(audio_bytes) else _wrap_pcm_in_wav(audio_bytes)

    client = _get_client(cfg.api_key_env, api_key)
    try:
        response = await client.aio.models.generate_content(
            model=cfg.model,
            contents=[cfg.prompt, _audio_part(payload)],
        )
    except GeminiSTTError:
        raise
    except Exception as exc:
        raise GeminiSTTError(f"Gemini STT call failed: {exc}") from exc

    text = getattr(response, "text", None)
    if text is None:
        # Defensive: some SDK versions surface text only via response.candidates;
        # fall back to that path before declaring an empty transcript.
        try:
            candidates = getattr(response, "candidates", []) or []
            if candidates:
                parts = getattr(candidates[0].content, "parts", []) or []
                text = "".join(getattr(p, "text", "") or "" for p in parts)
        except Exception:
            text = None
    return (text or "").strip()
