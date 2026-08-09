"""Piper TTS — local CPU-only synthesis via ONNX.

Renders text against a `.onnx` voice model + its sibling `.onnx.json`
config. Two operator-locked synthesis presets feed in via `cfg.presets`,
keyed by the `<intent>`/`<answer>` tag the chunked text emitter labels
each segment with: intent → quick + deterministic, answer → natural
pace + micro-variability. The assistant itself never picks a preset — the kind
flows through the rendering pipeline.

The PiperVoice handle is loaded lazily and cached per ONNX path so the
~200 ms ONNX init only happens once. `synthesize()` is async-friendly:
the synchronous `voice.synthesize(...)` call runs inside
`asyncio.to_thread` so it never blocks the event loop.

No network, no API key, no daily cap. Cost ledger stays wired ($0/M
chars) so the spend rollup includes Piper as a zero-row.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

_STYLE_CUE_RE = re.compile(r"\[[^\]]*\]")
_MARKDOWN_EMPHASIS_RE = re.compile(r"\*+")

_DEFAULT_PRESET = "answer"


@dataclass(frozen=True)
class PiperPreset:
    length_scale: float
    noise_scale: float
    noise_w: float
    sentence_silence: float


@dataclass(frozen=True)
class PiperTTSConfig:
    model_path: Path     # absolute path to the .onnx file
    config_path: Path    # absolute path to the .onnx.json file
    sample_rate: int
    presets: Mapping[str, PiperPreset] = field(default_factory=dict)
    preload: bool = False
    # Warm-up cap, from `roles.yaml::voice.tts.settings`. Present for the
    # same reason Kokoro's is: without it the engine used a hardcoded 60
    # while the config asked for 30, so the lane's preload could run for
    # twice what the operator had written down.
    timeout_seconds: float = 60.0


class PiperTTSError(RuntimeError):
    """Wraps any failure path so the engine surfaces a single error type."""


_voice_cache: dict[str, Any] = {}
_voice_factory: Callable[[Path, Path], Any] | None = None


def set_voice_factory(factory: Callable[[Path, Path], Any] | None) -> None:
    """Override the default `PiperVoice.load(...)` constructor for tests."""
    global _voice_factory
    _voice_factory = factory
    _voice_cache.clear()


def unload_models() -> None:
    """Drop cached PiperVoice handles. The ORT session releases its native
    arena on garbage-collection of the underlying object."""
    _voice_cache.clear()


def status(cfg: PiperTTSConfig | None) -> dict[str, Any]:
    """Mirror Settings shape — keys parallel local_whisper.status() so the
    LocalModels panel can render Piper alongside Whisper."""
    loaded = bool(_voice_cache)
    cached = [{"model_path": key} for key in _voice_cache]
    return {
        "configured": cfg is not None,
        "model_path": str(cfg.model_path) if cfg is not None else "",
        "config_path": str(cfg.config_path) if cfg is not None else "",
        "sample_rate": cfg.sample_rate if cfg is not None else None,
        "preload": cfg.preload if cfg is not None else False,
        "presets": sorted(cfg.presets.keys()) if cfg is not None else [],
        "loaded": loaded,
        "cached": cached,
    }


async def warm_up(cfg: PiperTTSConfig) -> None:
    """Load the configured voice and run a tiny synthesis to warm ORT.

    Mirror startup invokes this so the first spoken sentence doesn't pay
    the ~200 ms ONNX init latency. Failures bubble up so the engine can
    latch a `disabled_reason` and move to the next lane in the chain."""
    voice = await asyncio.to_thread(_load_voice, cfg.model_path, cfg.config_path)
    preset = _resolve_preset(cfg, _DEFAULT_PRESET)
    await asyncio.to_thread(_synthesize_blocking, voice, "ok", preset, cfg.sample_rate)


def _resolve_preset(cfg: PiperTTSConfig, preset_key: str | None) -> PiperPreset:
    """Pick a synthesis preset. Falls back to `answer` then to safe defaults
    so a missing-preset YAML edit can't kill synthesis mid-turn."""
    if preset_key and preset_key in cfg.presets:
        return cfg.presets[preset_key]
    if _DEFAULT_PRESET in cfg.presets:
        return cfg.presets[_DEFAULT_PRESET]
    if cfg.presets:
        return next(iter(cfg.presets.values()))
    return PiperPreset(length_scale=1.0, noise_scale=0.0, noise_w=0.0, sentence_silence=0.2)


def _load_voice(model_path: Path, config_path: Path) -> Any:
    """One PiperVoice per ONNX path. Module-level cache survives across calls
    so the model only goes through ORT init once."""
    key = str(model_path.resolve())
    cached = _voice_cache.get(key)
    if cached is not None:
        return cached
    if not model_path.exists():
        # Name the file the catalog actually asked for. A hardcoded voice
        # here sends the operator to fetch the wrong model the moment the
        # `local.piper.*` entry names a different one.
        raise PiperTTSError(
            f"Piper model not found at {model_path}. Fetch it with "
            "`python -m tesseract.scripts.fetch_piper_voice`, or download "
            f"{model_path.name} + {config_path.name} from "
            "https://huggingface.co/rhasspy/piper-voices and place both "
            f"files in {model_path.parent}/"
        )
    if not config_path.exists():
        raise PiperTTSError(
            f"Piper voice config not found at {config_path} — Piper requires "
            "the .onnx.json file alongside the .onnx file"
        )
    if _voice_factory is not None:
        voice = _voice_factory(model_path, config_path)
    else:
        try:
            from piper import PiperVoice  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dep
            raise PiperTTSError(
                "piper-tts not installed — `pip install piper-tts` "
                "(see tesseract/pyproject.toml [voice-local] extra)"
            ) from exc
        voice = PiperVoice.load(str(model_path), config_path=str(config_path), use_cuda=False)
    _voice_cache[key] = voice
    return voice


def _sanitize_for_tts(text: str) -> str:
    """Same sanitisation contract as kokoro_tts — strip bracketed style cues
    (`[whispers]`) and markdown emphasis (`*`, `**`) so they don't surface as
    spoken artefacts. Whitespace from removed tokens collapses to one space."""
    cleaned = _STYLE_CUE_RE.sub("", text)
    cleaned = _MARKDOWN_EMPHASIS_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _synthesize_blocking(
    voice: Any,
    text: str,
    preset: PiperPreset,
    sample_rate: int,
) -> bytes:
    """Synchronous synth path — owned by `asyncio.to_thread` so it can't
    stall the event loop. Returns a fully-wrapped WAV buffer."""
    try:
        from piper import SynthesisConfig  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise PiperTTSError("piper-tts missing SynthesisConfig") from exc

    syn_cfg = SynthesisConfig(
        length_scale=preset.length_scale,
        noise_scale=preset.noise_scale,
        noise_w_scale=preset.noise_w,
    )
    pcm = bytearray()
    try:
        for chunk in voice.synthesize(text, syn_config=syn_cfg):
            data = getattr(chunk, "audio_int16_bytes", None)
            if data is None:
                # Older piper-tts shape — `audio_float_array` exposed instead.
                # The runtime targets piper-tts >= 1.2 which guarantees
                # `audio_int16_bytes`; raise loudly so the operator upgrades.
                raise PiperTTSError(
                    "Piper chunk missing `audio_int16_bytes` — upgrade piper-tts to >=1.2"
                )
            pcm.extend(data)
    except PiperTTSError:
        raise
    except Exception as exc:
        raise PiperTTSError(f"Piper synth failed: {exc}") from exc

    # Inject inter-segment silence so paragraph splits keep a beat. The
    # caller passes one segment at a time, so this padding only fires at
    # the trailing edge of each chunk.
    silence_samples = int(preset.sentence_silence * sample_rate)
    if silence_samples > 0:
        pcm.extend(b"\x00\x00" * silence_samples)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(pcm))
    return buf.getvalue()


async def synthesize(
    text: str,
    cfg: PiperTTSConfig,
    *,
    preset: str | None = None,
) -> bytes:
    """Render `text` to a WAV blob using the named preset.

    `preset` is the `<intent>` / `<answer>` kind from the chunked text
    emitter; unknown values fall back to `answer`. Empty / whitespace
    text returns empty bytes (no debit).
    """
    if not text.strip():
        return b""
    spoken_text = _sanitize_for_tts(text)
    if not spoken_text:
        return b""

    # Cache hit fast path — a dict lookup is event-loop safe. On a miss
    # (e.g. operator hit Unload from Settings, or first call before
    # warm_up has finished), `_load_voice` triggers PiperVoice.load —
    # ~200 ms of ONNX init. Push that to a worker thread so the WS
    # event loop never stalls.
    cache_key = str(cfg.model_path.resolve())
    voice = _voice_cache.get(cache_key)
    if voice is None:
        voice = await asyncio.to_thread(_load_voice, cfg.model_path, cfg.config_path)
    chosen = _resolve_preset(cfg, preset)
    return await asyncio.to_thread(
        _synthesize_blocking, voice, spoken_text, chosen, cfg.sample_rate
    )
