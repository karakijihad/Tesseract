"""Gemini text-to-speech over the Interactions API, streamed.

The cloud speaking lane. Local synthesis is free and fast once its model
is on disk; this one exists so a machine that declined the local voice —
or has no disk to spare for it — still speaks.

It streams by necessity, not preference. Asked for one blob the endpoint
returns nothing until the whole utterance is rendered, which costs more
wall-clock the more there is to say; streamed, the first audio arrives at
a flat ~1.2 s whatever the reply length. A voice lane that gets slower as
the answer gets longer is not usable for a spoken turn.

Transport is `httpx` against `POST /v1beta/interactions` rather than the
`google-genai` client the STT sibling uses. Two reasons: the SSE surface
is the whole point here and raw requests keep it in view, and
`image_generate.py` already talks to this same endpoint the same way. The
shape of this module still follows `providers/gemini.py` — frozen
dataclass config, an injectable client factory for tests, one error type.

The endpoint emits raw little-endian 16-bit mono PCM at 24 kHz. Every
other lane in the system hands back WAV (`voice/encode.py` demuxes it,
the browser player decodes it), so the PCM is wrapped here and the
container boundary stays inside this file.

Style is a natural-language preamble on the input text, so it maps onto
the same per-surface `synthesis_presets` contract the local lanes use:
`intent` and `answer` name their own style and pace in the catalog entry,
and nothing at runtime can reach them. Accent is deliberately not a knob
— the shipped voices are neutral, per the 2026-08-10 decision.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import wave
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

_PCM_BITS_PER_SAMPLE = 16
_PCM_CHANNELS = 1
_DEFAULT_PRESET = "answer"

# The Interactions API pins its request/response contract to a dated
# revision, and streaming is only offered on one recent enough to carry
# it. Sending no revision header gets the account's default, which is not
# ours to assume — an account defaulted to an older revision would answer
# a streaming request with a blocking response and this lane would look
# merely slow rather than misconfigured.
_API_REVISION = "2026-05-20"

_SSE_DATA_PREFIX = "data:"
_SSE_DONE = "[DONE]"


@dataclass(frozen=True)
class GeminiPreset:
    """One surface's delivery. Both fields are prose because the model
    takes prose — they are enumerated in the catalog, not free text a
    caller composes."""

    style: str = ""
    pace: str = ""


@dataclass(frozen=True)
class GeminiTTSConfig:
    model: str            # e.g. "gemini-3.1-flash-tts-preview"
    api_key_env: str      # e.g. "GOOGLE_API_KEY"
    base_url: str         # full Interactions endpoint
    voice: str            # catalog voice name, e.g. "Erinome"
    timeout_seconds: float
    sample_rate: int = 24_000
    # One line of identity, set once by the operator. Not per-utterance:
    # a voice that drifts turn to turn is the thing the preset contract
    # exists to prevent.
    audio_profile: str = ""
    presets: Mapping[str, GeminiPreset] = field(default_factory=dict)


class GeminiTTSError(RuntimeError):
    """Wraps every failure path so the engine sees a single error type and
    can latch the lane off with one reason string."""


_ClientFactory = Callable[[float], Any]
_client_factory: _ClientFactory | None = None
_client_cache: dict[float, Any] = {}


def set_client_factory(factory: _ClientFactory | None) -> None:
    """Override the `httpx.AsyncClient` constructor.

    Tests pass a stub whose `.stream(...)` yields canned SSE lines, so no
    test in this repo reaches the network. `None` restores the real path.
    Clearing the cache is part of the swap — a stub left pooled would
    outlive the test that installed it.
    """
    global _client_factory
    _client_factory = factory
    _client_cache.clear()


def _default_factory(timeout_seconds: float) -> Any:
    try:
        from tesseract import http_client
    except ImportError as exc:  # pragma: no cover - hard dep in pyproject
        raise GeminiTTSError("httpx not installed") from exc
    return http_client.async_client(timeout=timeout_seconds)


def _get_client(timeout_seconds: float) -> Any:
    """One pooled client per timeout, held for the process.

    Deliberately not a client per call. A spoken turn synthesises
    sentence by sentence, so a fresh client would pay a TCP and TLS
    handshake for every sentence — on the one lane whose whole argument is
    how quickly the first audio arrives. Pooling keeps the connection warm
    across a turn and across turns.

    Keyed on the timeout because that is the only constructor argument;
    the key set is the distinct timeouts in the operator's config, which
    is one.
    """
    cached = _client_cache.get(timeout_seconds)
    if cached is not None:
        return cached
    factory = _client_factory or _default_factory
    client = factory(timeout_seconds)
    _client_cache[timeout_seconds] = client
    return client


def status(cfg: GeminiTTSConfig | None) -> dict[str, Any]:
    """Mirror Settings shape — keys parallel `kokoro_tts.status()`.

    `loaded` / `cached` are reported as the empty case rather than
    omitted: this lane holds no model, and a panel that renders one lane's
    keys and not another's would read as a lane that failed to report.
    `key_present` is the one thing worth knowing about a cloud lane before
    it is asked to speak.
    """
    return {
        "configured": cfg is not None,
        "model": cfg.model if cfg is not None else "",
        "voice": cfg.voice if cfg is not None else "",
        "api_key_env": cfg.api_key_env if cfg is not None else "",
        "key_present": bool(os.environ.get(cfg.api_key_env)) if cfg is not None else False,
        "sample_rate": cfg.sample_rate if cfg is not None else None,
        "presets": sorted(cfg.presets.keys()) if cfg is not None else [],
        "loaded": False,
        "cached": [],
    }


def _resolve_preset(cfg: GeminiTTSConfig, preset_key: str | None) -> GeminiPreset:
    """Same fallback ladder the local lanes use: the asked-for surface,
    then `answer`, then whatever the entry defines, then nothing."""
    if preset_key and preset_key in cfg.presets:
        return cfg.presets[preset_key]
    if _DEFAULT_PRESET in cfg.presets:
        return cfg.presets[_DEFAULT_PRESET]
    if cfg.presets:
        return next(iter(cfg.presets.values()))
    return GeminiPreset()


def _direction(cfg: GeminiTTSConfig, preset: GeminiPreset) -> str:
    """The preamble prefixed to the spoken text.

    Empty when the entry sets neither a profile nor a preset, in which
    case the text is sent bare — an empty instruction line would spend
    input tokens telling the model nothing.
    """
    parts: list[str] = []
    if cfg.audio_profile.strip():
        parts.append(cfg.audio_profile.strip())
    if preset.style.strip():
        parts.append(f"Style: {preset.style.strip()}.")
    if preset.pace.strip():
        parts.append(f"Pace: {preset.pace.strip()}.")
    return " ".join(parts)


def _wrap_pcm_in_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(_PCM_CHANNELS)
        w.setsampwidth(_PCM_BITS_PER_SAMPLE // 8)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _audio_from_event(payload: Any) -> bytes:
    """Pull the PCM out of one decoded SSE event, or b"" if it carries none.

    Only `step.delta` events with an audio delta hold audio; the stream
    also carries lifecycle events, and a thought/text delta on a TTS model
    would decode to bytes that are not sound. Anything unrecognised is
    skipped rather than guessed at.
    """
    if not isinstance(payload, dict):
        return b""
    if payload.get("event_type") != "step.delta":
        return b""
    delta = payload.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "audio":
        return b""
    data = delta.get("data")
    if not isinstance(data, str) or not data:
        return b""
    try:
        return base64.b64decode(data)
    except (ValueError, TypeError) as exc:
        raise GeminiTTSError(f"Gemini TTS sent undecodable audio: {exc}") from exc


def _request_body(text: str, cfg: GeminiTTSConfig, preset: GeminiPreset) -> dict[str, Any]:
    direction = _direction(cfg, preset)
    return {
        "model": cfg.model,
        "input": f"{direction} {text}".strip() if direction else text,
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": cfg.voice}]},
        "stream": True,
    }


async def synthesize(
    text: str,
    cfg: GeminiTTSConfig | None,
    *,
    preset: str = _DEFAULT_PRESET,
) -> bytes:
    """Render `text` to WAV bytes over the streamed Interactions API.

    Returns empty bytes for empty input — the engine treats that as a
    no-op and never debits for it. Raises `GeminiTTSError` on a missing
    key, a transport failure, an HTTP error, or a stream that completes
    without producing any audio; the engine latches the lane off on any
    of them and falls to the next.

    The caller owns metering, as it does for every other lane: this
    module stays pure IO so `TTSEngine` remains the single place voice
    spend is recorded.
    """
    if cfg is None:
        raise GeminiTTSError("gemini TTS lane is not configured")
    if not text.strip():
        return b""

    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        raise GeminiTTSError(
            f"{cfg.api_key_env} not set in environment (add to tesseract/.env)"
        )

    body = _request_body(text, cfg, _resolve_preset(cfg, preset))
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "Api-Revision": _API_REVISION,
    }

    client = _get_client(cfg.timeout_seconds)

    chunks: list[bytes] = []
    try:
        async with client.stream(
            "POST", cfg.base_url, json=body, headers=headers,
        ) as response:
            if response.status_code >= 400:
                # The body is the only place the API says WHY, and on a
                # streamed response it has not been read yet. Without this
                # the operator gets a bare status code for a missing key,
                # a retired preview model, and an exhausted quota alike.
                detail = await response.aread()
                raise GeminiTTSError(
                    f"Gemini TTS HTTP {response.status_code}: "
                    f"{detail.decode('utf-8', 'replace')[:300]}"
                )
            async for line in response.aiter_lines():
                if not line.startswith(_SSE_DATA_PREFIX):
                    continue
                data = line[len(_SSE_DATA_PREFIX):].strip()
                if not data or data == _SSE_DONE:
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    # One malformed frame must not lose the utterance
                    # already streamed; the empty-audio check below is
                    # what decides whether the result is usable.
                    logger.warning("gemini TTS: skipping unparseable SSE frame")
                    continue
                audio = _audio_from_event(payload)
                if audio:
                    chunks.append(audio)
    except GeminiTTSError:
        raise
    except Exception as exc:
        raise GeminiTTSError(f"Gemini TTS call failed: {exc}") from exc

    pcm = b"".join(chunks)
    if not pcm:
        # A 200 that produced no audio is a failure, not silence. Returning
        # b"" here would hand the engine an empty utterance it would record
        # as a success, and the turn would go quiet with nothing logged.
        raise GeminiTTSError("Gemini TTS stream completed with no audio")
    return _wrap_pcm_in_wav(pcm, cfg.sample_rate)
