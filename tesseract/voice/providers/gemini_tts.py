"""Gemini 2.5 Flash TTS via google-genai.

Wraps `client.aio.models.generate_content(...)` with a single text part
and `responseModalities=["AUDIO"]` + a prebuilt `voice_config`. Returns
WAV-wrapped 24 kHz / 16-bit / mono PCM bytes ready for the frontend
`AudioContext.decodeAudioData` path.

Style is **prompted** via the documented Director's-Notes pattern: the
`contents` string is `"<style_prompt>: <transcript>"`. Gemini-TTS treats
the prefix before the colon as instruction (NOT spoken) and renders the
transcript in that style. Per-surface style prompts (`intent` / `answer`)
live in `roles.yaml::voice.tts.settings.api.google.gemini_flash_tts.synthesis_presets`
and are operator-locked — no per-turn variation, no agent-side knob.

Voice timbre is picked via `voice_id` (e.g. `Charon`).

The `google-genai` Client carries a small lifetime cost (transport +
auth init) so we cache one per process keyed on the env-var **name**
(matches `gemini.py` STT). Tests inject a fake client via
`set_client_factory()` to avoid hitting the network.
"""

from __future__ import annotations

import io
import logging
import os
import re
import wave
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

# Gemini Flash TTS treats square-bracketed tokens (`[laughs]`,
# `[whispers]`, `[sighs]`) as in-line style cues — the model inflects
# accordingly. If TARS happens to emit any `[word]` shape (tool-call
# residue, parenthetical-as-bracket, copy-paste from logs) the sentence
# containing it gets a spurious tone shift. Markdown asterisks similarly
# trigger emphasis inflection. Strip both before synthesis so the spoken
# text is pure prose. Backslash-bracket / nested-bracket cases are rare
# enough to ignore — the regex prefers under-stripping over swallowing
# legitimate punctuation.
_STYLE_CUE_RE = re.compile(r"\[[^\]]*\]")
_MARKDOWN_EMPHASIS_RE = re.compile(r"\*+")

logger = logging.getLogger(__name__)

# Gemini TTS emits 24 kHz / 16-bit / mono PCM. The frontend's
# AudioContext decoder needs a real RIFF envelope; we wrap here so the
# wire shape is consistent with the STT-side WAV path.
_PCM_SAMPLE_RATE_HZ = 24_000
_PCM_BITS_PER_SAMPLE = 16
_PCM_CHANNELS = 1


_DEFAULT_PRESET = "answer"


@dataclass(frozen=True)
class GeminiPreset:
    """Per-surface style instruction. Gemini interprets the prefix
    before the colon in `contents` as Director's Notes and does not
    speak it — see module docstring."""

    style_prompt: str


@dataclass(frozen=True)
class GeminiTTSConfig:
    model: str          # e.g. "gemini-2.5-flash-preview-tts"
    api_key_env: str    # e.g. "GOOGLE_API_KEY"
    voice_id: str       # default voice (overridable per call)
    timeout_seconds: float
    presets: Mapping[str, GeminiPreset] = field(default_factory=dict)


class GeminiTTSError(RuntimeError):
    """Wraps any failure path so the engine surfaces a single error type."""


_ClientFactory = Callable[[str], Any]
_GenerateFn = Callable[..., Awaitable[Any]]

_client_cache: dict[str, Any] = {}
_client_factory: _ClientFactory | None = None


def set_client_factory(factory: _ClientFactory | None) -> None:
    """Override the default `genai.Client(api_key=...)` constructor.

    Tests pass a stub returning an object with
    `.aio.models.generate_content`. Pass `None` to restore the real path."""
    global _client_factory
    _client_factory = factory
    _client_cache.clear()


def _default_factory(api_key: str) -> Any:
    try:
        from google import genai  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - hard dep in pyproject
        raise GeminiTTSError(
            "google-genai not installed — `pip install google-genai`"
        ) from exc
    return genai.Client(api_key=api_key)


def _get_client(api_key_env: str, api_key: str) -> Any:
    """One client per env-var slot. Rotation is handled by
    `set_client_factory(None)` which clears the cache."""
    cached = _client_cache.get(api_key_env)
    if cached is not None:
        return cached
    factory = _client_factory or _default_factory
    client = factory(api_key)
    _client_cache[api_key_env] = client
    return client


def _wrap_pcm_in_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(_PCM_CHANNELS)
        w.setsampwidth(_PCM_BITS_PER_SAMPLE // 8)
        w.setframerate(_PCM_SAMPLE_RATE_HZ)
        w.writeframes(pcm)
    return buf.getvalue()


def _build_speech_config(voice_id: str) -> Any:
    """Build the `SpeechConfig(voice_config=PrebuiltVoiceConfig(voice_name))`
    object Gemini TTS expects on the `config.speech_config` slot."""
    try:
        from google.genai import types  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - hard dep in pyproject
        raise GeminiTTSError("google-genai missing types module") from exc
    return types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_id),
        ),
    )


def _build_generation_config(voice_id: str) -> Any:
    try:
        from google.genai import types  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise GeminiTTSError("google-genai missing types module") from exc
    return types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=_build_speech_config(voice_id),
    )


def _sanitize_for_tts(text: str) -> str:
    """Strip in-line style cues (`[whispers]`) and markdown emphasis
    (`**bold**`, `*italic*`) before sending to Gemini TTS so neither
    triggers a mid-sentence inflection shift. Whitespace from removed
    tokens collapses to a single space so the prosody isn't broken."""
    cleaned = _STYLE_CUE_RE.sub("", text)
    cleaned = _MARKDOWN_EMPHASIS_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_audio_bytes(response: Any) -> bytes:
    """Pull raw PCM bytes off a `generate_content` response. The audio
    sits at `response.candidates[0].content.parts[0].inline_data.data`.
    Some SDK shapes also expose `.data` directly on the part — try both."""
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return b""
        parts = getattr(candidates[0].content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None:
                data = getattr(inline, "data", None)
                if isinstance(data, (bytes, bytearray)):
                    return bytes(data)
            data = getattr(part, "data", None)
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
    except Exception as exc:
        raise GeminiTTSError(f"unexpected response shape: {exc}") from exc
    return b""


def _resolve_preset(cfg: GeminiTTSConfig, preset_key: str | None) -> GeminiPreset | None:
    """Pick a per-surface preset. Falls back to `answer` if `preset_key`
    is missing. Returns None when no presets are configured at all
    (in which case the call sends `contents = transcript` only)."""
    if preset_key and preset_key in cfg.presets:
        return cfg.presets[preset_key]
    if _DEFAULT_PRESET in cfg.presets:
        return cfg.presets[_DEFAULT_PRESET]
    if cfg.presets:
        return next(iter(cfg.presets.values()))
    return None


async def synthesize(
    text: str,
    cfg: GeminiTTSConfig,
    *,
    voice_id: str | None = None,
    preset: str | None = None,
) -> bytes:
    """Render `text` to audio via Gemini Flash TTS. Returns WAV bytes.

    `contents` is built as `"<style_prompt>: <transcript>"` when a
    preset is configured (Director's-Notes pattern — the prefix is
    instruction, not speech), or just `<transcript>` otherwise. The
    caller is expected to have already debited the cost ledger — this
    provider stays pure-IO so `TTSEngine` owns the metering invariant."""
    if not text.strip():
        return b""

    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        raise GeminiTTSError(
            f"{cfg.api_key_env} not set in environment (add to tesseract/.env)"
        )

    chosen_voice = voice_id or cfg.voice_id
    spoken_text = _sanitize_for_tts(text)
    if not spoken_text:
        return b""

    chosen_preset = _resolve_preset(cfg, preset)
    if chosen_preset and chosen_preset.style_prompt.strip():
        effective_text = f"{chosen_preset.style_prompt.strip()}: {spoken_text}"
    else:
        effective_text = spoken_text

    client = _get_client(cfg.api_key_env, api_key)
    try:
        response = await client.aio.models.generate_content(
            model=cfg.model,
            contents=effective_text,
            config=_build_generation_config(chosen_voice),
        )
    except GeminiTTSError:
        raise
    except Exception as exc:
        raise GeminiTTSError(f"Gemini TTS call failed: {exc}") from exc

    pcm = _extract_audio_bytes(response)
    if not pcm:
        raise GeminiTTSError("Gemini TTS response missing audio payload")
    return _wrap_pcm_in_wav(pcm)
