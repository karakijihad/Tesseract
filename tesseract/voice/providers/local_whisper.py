"""Local faster-whisper transcription provider."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import threading
import time
import wave
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_PCM_SAMPLE_RATE_HZ = 16_000
_model_cache: dict[tuple[str, str, str], Any] = {}
# Config key → (device, compute_type) actually loaded. Diverges from the key
# when a cuda-configured load fell back to CPU — status() must report what's
# really running, not what the config asked for (review finding 2026-07-30).
_loaded_params: dict[tuple[str, str, str], tuple[str, str]] = {}
_model_locks: dict[tuple[str, str, str], threading.Lock] = {}
_model_locks_guard = threading.Lock()
_model_factory = None
_dll_directory_handles: list[Any] = []


@dataclass(frozen=True)
class LocalWhisperConfig:
    provider: str
    model: str
    device: str
    compute_type: str
    language: str | None
    beam_size: int
    timeout_seconds: float = 20.0
    preload: bool = False


class LocalWhisperError(RuntimeError):
    """Wraps local STT failures so callers can fall back cleanly."""


def set_model_factory(factory) -> None:
    global _model_factory
    _model_factory = factory
    _model_cache.clear()
    _loaded_params.clear()
    _model_locks.clear()


def unload_models() -> None:
    """Drop cached Whisper model handles.

    faster-whisper runs in-process, unlike Ollama's external daemon. On
    Mirror shutdown/config reload, clearing the cache releases our Python
    references; the process exit then releases CUDA memory deterministically.
    """
    _model_cache.clear()
    _loaded_params.clear()
    _model_locks.clear()


def status(cfg: LocalWhisperConfig | None) -> dict[str, Any]:
    loaded = bool(_model_cache)
    cached = []
    for key in _model_cache:
        model, device, compute_type = key
        actual_device, actual_compute = _loaded_params.get(key, (device, compute_type))
        cached.append(
            {"model": model, "device": actual_device, "compute_type": actual_compute}
        )
    return {
        "configured": cfg is not None,
        "provider": cfg.provider if cfg is not None else "",
        "model": cfg.model if cfg is not None else "",
        "device": cfg.device if cfg is not None else "",
        "compute_type": cfg.compute_type if cfg is not None else "",
        "language": cfg.language if cfg is not None else None,
        "timeout_seconds": cfg.timeout_seconds if cfg is not None else None,
        "preload": cfg.preload if cfg is not None else False,
        "loaded": loaded,
        "cached": cached,
    }


def _default_factory(model: str, device: str, compute_type: str) -> Any:
    _ensure_cuda_dll_dirs(device)
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LocalWhisperError(
            "faster-whisper not installed - install `faster-whisper` for local STT"
        ) from exc
    return WhisperModel(model, device=device, compute_type=compute_type)


def _ensure_cuda_dll_dirs(device: str) -> None:
    if device.lower() != "cuda" or sys.platform != "win32":
        return
    if _dll_directory_handles:
        return
    for package in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc"):
        # find_spec("nvidia.x") imports the parent "nvidia" package first and
        # raises ModuleNotFoundError when it's absent (no [gpu] extra installed)
        # — that's the normal CPU-only case, not an error (found live 2026-07-30).
        try:
            spec = find_spec(package)
        except ModuleNotFoundError:
            return
        locations = list(spec.submodule_search_locations or []) if spec else []
        for location in locations:
            bin_dir = os.path.join(location, "bin")
            if not os.path.isdir(bin_dir):
                continue
            try:
                path_entries = os.environ.get("PATH", "").split(os.pathsep)
                if bin_dir not in path_entries:
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                _dll_directory_handles.append(os.add_dll_directory(bin_dir))
                logger.info("local Whisper added CUDA DLL directory: %s", bin_dir)
            except (FileNotFoundError, OSError):
                logger.exception("local Whisper failed to add CUDA DLL directory: %s", bin_dir)


def get_model(cfg: LocalWhisperConfig) -> Any:
    """Return the cached faster-whisper model for ``cfg``.

    Channel adapters (CR-2) need to call ``model.transcribe`` against a file path
    so PyAV/ffmpeg handles arbitrary containers — this keeps the cache singular.
    """
    return _get_model(cfg)


def _get_model(cfg: LocalWhisperConfig) -> Any:
    key = (cfg.model, cfg.device, cfg.compute_type)
    cached = _model_cache.get(key)
    if cached is not None:
        return cached
    with _model_locks_guard:
        lock = _model_locks.setdefault(key, threading.Lock())
    with lock:
        cached = _model_cache.get(key)
        if cached is not None:
            return cached
        factory = _model_factory or _default_factory
        started = time.perf_counter()
        device, compute_type = cfg.device, cfg.compute_type
        try:
            model = factory(cfg.model, device, compute_type)
        except Exception as exc:
            if device.lower() == "cpu":
                raise
            # providers.yaml promises "falls back to CPU if CUDA missing" —
            # honour it: a machine without the CUDA stack (no [gpu] extra,
            # no NVIDIA driver) must still get local STT rather than an
            # every-boot failure. Cached under the config's key so the
            # cuda attempt isn't repeated per transcription.
            device, compute_type = "cpu", "int8"
            logger.warning(
                "local Whisper %s load failed (%s); falling back to device=cpu compute=int8",
                cfg.device,
                exc,
            )
            model = factory(cfg.model, device, compute_type)
        logger.info(
            "local Whisper loaded model=%s device=%s compute=%s in %.2fs",
            cfg.model,
            device,
            compute_type,
            time.perf_counter() - started,
        )
        _model_cache[key] = model
        _loaded_params[key] = (device, compute_type)
        return model


def _pcm_to_float32(audio_bytes: bytes) -> np.ndarray:
    if not audio_bytes:
        return np.zeros(0, dtype=np.float32)

    pcm = audio_bytes
    sample_rate = _PCM_SAMPLE_RATE_HZ
    if len(audio_bytes) >= 12 and audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
                sample_rate = wav.getframerate()
                channels = wav.getnchannels()
                width = wav.getsampwidth()
                if width != 2:
                    raise LocalWhisperError("local STT only supports 16-bit PCM WAV")
                raw = wav.readframes(wav.getnframes())
                arr = np.frombuffer(raw, dtype="<i2")
                if channels > 1:
                    arr = arr.reshape(-1, channels).mean(axis=1).astype(np.int16)
                pcm = arr.tobytes()
        except LocalWhisperError:
            raise
        except Exception as exc:
            raise LocalWhisperError(f"failed to parse WAV audio: {exc}") from exc

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if sample_rate != _PCM_SAMPLE_RATE_HZ:
        logger.warning("local STT received %s Hz audio; expected 16000 Hz", sample_rate)
    return samples


async def transcribe(audio_bytes: bytes, cfg: LocalWhisperConfig) -> str:
    if not audio_bytes:
        return ""

    samples = _pcm_to_float32(audio_bytes)
    if samples.size == 0:
        return ""

    def run() -> str:
        started = time.perf_counter()
        model = _get_model(cfg)
        segments, _info = model.transcribe(
            samples,
            language=cfg.language,
            beam_size=cfg.beam_size,
            vad_filter=True,
        )
        text = " ".join(
            seg.text.strip() for seg in segments if getattr(seg, "text", "").strip()
        ).strip()
        logger.info(
            "local Whisper transcribed %.2fs audio in %.2fs chars=%d",
            samples.size / _PCM_SAMPLE_RATE_HZ,
            time.perf_counter() - started,
            len(text),
        )
        return text

    try:
        return await asyncio.to_thread(run)
    except LocalWhisperError:
        raise
    except Exception as exc:
        raise LocalWhisperError(f"local Whisper STT failed: {exc}") from exc


async def warm_up(cfg: LocalWhisperConfig) -> None:
    """Load the configured model and exercise one tiny transcription."""
    def run() -> None:
        model = _get_model(cfg)
        samples = np.zeros(_PCM_SAMPLE_RATE_HZ // 10, dtype=np.float32)
        segments, _info = model.transcribe(
            samples,
            language=cfg.language or "en",
            beam_size=cfg.beam_size,
            vad_filter=True,
        )
        for _segment in segments:
            pass

    await asyncio.to_thread(run)
