"""Kokoro TTS — local 24 kHz synthesis via onnxruntime (GPU or CPU).

Single 311 MB ONNX model, 54 voices bundled in `voices-v1.0.bin`. The
voice is a *style embedding*; multiple embeddings can be combined as a
linear blend to design a custom timbre. The blend recipe lives in
`providers.yaml::local.kokoro.<model>.mix` (e.g. `am_eric: 0.5,
bm_daniel: 0.5`); the operator iterates the blend in YAML, no code edit.

Two operator-locked synthesis presets feed in via `cfg.presets`, keyed
by the `<intent>` / `<answer>` tag the chunked text emitter labels each
segment with. Kokoro exposes only `speed` (and post-pad silence); the
preset shapes those two knobs.

GPU path. `onnxruntime-gpu` requires the CUDA 12 runtime DLLs to be
findable at load time. On Windows the pip-installed `nvidia-*-cu12`
wheels live under `<venv>/Lib/site-packages/nvidia/<lib>/bin/` — adding
those dirs to the DLL search path AND prepending `PATH` is the only
reliable way to satisfy onnxruntime's secondary DLL loads. Falls back
to CPUExecutionProvider cleanly if CUDA isn't available.

The `Kokoro` handle and ORT session are cached per
(model_path, voices_path, device) so the ~2-5 s init cost only fires
once per Mirror lifecycle. `synthesize()` runs the synchronous `create()`
inside `asyncio.to_thread` so it never stalls the event loop.

No network, no API key, no daily cap. Cost ledger stays wired ($0/M
chars) so the spend rollup includes Kokoro as a zero-row.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sys
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_STYLE_CUE_RE = re.compile(r"\[[^\]]*\]")
_MARKDOWN_EMPHASIS_RE = re.compile(r"\*+")

_DEFAULT_PRESET = "answer"
_KOKORO_SAMPLE_RATE = 24000  # fixed by the model


@dataclass(frozen=True)
class KokoroPreset:
    speed: float
    sentence_silence: float


@dataclass(frozen=True)
class KokoroTTSConfig:
    model_path: Path             # absolute path to kokoro-v1.0.onnx
    voices_path: Path            # absolute path to voices-v1.0.bin
    mix: Mapping[str, float]     # voice_id -> weight (e.g. {"bm_george": 0.6, ...})
    lang: str = "en-gb"
    device: str = "cuda"         # "cuda" | "cpu"
    sample_rate: int = _KOKORO_SAMPLE_RATE
    presets: Mapping[str, KokoroPreset] = field(default_factory=dict)
    preload: bool = False
    # Warm-up cap. Catalog default is the connection's `timeout_seconds`;
    # `_build_voice_runtime` plumbs it from `roles.yaml::voice.tts.settings`
    # so the operator-tunable value doesn't get hardcoded in Python.
    timeout_seconds: float = 60.0


class KokoroTTSError(RuntimeError):
    """Wraps any failure path so the engine surfaces a single error type."""


# (model_path, voices_path, device) -> {"kokoro": Kokoro, "session": ort.InferenceSession, "active_provider": str}
_kokoro_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

# mix signature -> np.ndarray style embedding (cached so re-blending is free)
_style_cache: dict[tuple[tuple[str, float], ...], Any] = {}

_cuda_dlls_registered = False
_cuda_dlls_lock = threading.Lock()


def _register_cuda_dlls() -> None:
    """Make the pip-installed CUDA 12 / cuDNN 9 DLLs visible to
    onnxruntime's CUDA provider before it loads.

    On Windows, three things together are needed because onnxruntime's
    provider DLL chain-loads its deps via `LoadLibraryEx` and Python's
    `os.add_dll_directory` doesn't reach those secondary loads:

      1. `os.add_dll_directory` for each `nvidia/*/bin` dir  — covers
         direct `import onnxruntime` paths.
      2. PATH prepend  — covers some legacy load mechanisms.
      3. `ctypes.WinDLL(<full_path>)` for the foundational libs
         (cudart, cublas, cublasLt, cuFFT, cuDNN graph)  — pins them
         in the process loader cache so subsequent name-only loads
         from inside onnxruntime resolve."""
    global _cuda_dlls_registered
    if _cuda_dlls_registered:
        return
    with _cuda_dlls_lock:
        if _cuda_dlls_registered:
            return
        _do_register_cuda_dlls()
        _cuda_dlls_registered = True


def _do_register_cuda_dlls() -> None:
    if sys.platform != "win32":
        return

    # `sysconfig.get_paths()['purelib']` is the correct site-packages
    # regardless of whether we're running under a venv (Scripts/python.exe
    # → ../Lib/site-packages) or the system install (python.exe directly
    # under the root → Lib/site-packages).
    import sysconfig

    venv_site = Path(sysconfig.get_paths()["purelib"])
    bin_dirs: list[Path] = []
    for sub in (
        "cublas/bin",
        "cudnn/bin",
        "cuda_nvrtc/bin",
        "cuda_runtime/bin",
        "cufft/bin",
        "curand/bin",
        "cusparse/bin",
        "cusolver/bin",
        "nvjitlink/bin",
    ):
        d = venv_site / "nvidia" / sub
        if d.is_dir():
            bin_dirs.append(d)
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(str(d))
                except OSError:
                    pass
    if bin_dirs:
        os.environ["PATH"] = (
            os.pathsep.join(str(d) for d in bin_dirs)
            + os.pathsep
            + os.environ.get("PATH", "")
        )

    # Preload the foundational CUDA libs by full path so they're resident
    # in the process loader cache when onnxruntime's CUDA provider DLL
    # is loaded later. Anything missing is a non-fatal warning — fall
    # back to CPU.
    import ctypes

    preload_targets = [
        ("cuda_runtime/bin", "cudart64_12.dll"),
        ("cublas/bin", "cublas64_12.dll"),
        ("cublas/bin", "cublasLt64_12.dll"),
        ("cufft/bin", "cufft64_11.dll"),
        ("curand/bin", "curand64_10.dll"),
        ("cusparse/bin", "cusparse64_12.dll"),
        ("cusolver/bin", "cusolver64_11.dll"),
        ("cudnn/bin", "cudnn_graph64_9.dll"),
        ("cudnn/bin", "cudnn64_9.dll"),
    ]
    loaded: list[str] = []
    missing: list[str] = []
    for sub, dll_name in preload_targets:
        full = venv_site / "nvidia" / sub / dll_name
        if not full.is_file():
            missing.append(dll_name)
            continue
        try:
            ctypes.WinDLL(str(full))
            loaded.append(dll_name)
        except OSError as exc:
            logger.debug("Kokoro: preload %s failed: %s", dll_name, exc)
            missing.append(dll_name)
    if missing:
        logger.info("Kokoro: CUDA preload missing %s — GPU may fall back to CPU", missing)
    else:
        logger.info("Kokoro: preloaded %d CUDA DLLs", len(loaded))


def status(cfg: KokoroTTSConfig | None) -> dict[str, Any]:
    """Mirror Settings shape — keys parallel the other lanes' status() so the
    LocalModels panel can render Kokoro alongside Whisper."""
    loaded = bool(_kokoro_cache)
    cached = []
    for key, entry in _kokoro_cache.items():
        cached.append({
            "model_path": key[0],
            "voices_path": key[1],
            "device": key[2],
            "provider": entry.get("active_provider", ""),
        })
    return {
        "configured": cfg is not None,
        "model_path": str(cfg.model_path) if cfg is not None else "",
        "voices_path": str(cfg.voices_path) if cfg is not None else "",
        "mix": dict(cfg.mix) if cfg is not None else {},
        "lang": cfg.lang if cfg is not None else "",
        "device": cfg.device if cfg is not None else "",
        "sample_rate": cfg.sample_rate if cfg is not None else None,
        "preload": cfg.preload if cfg is not None else False,
        "presets": sorted(cfg.presets.keys()) if cfg is not None else [],
        "loaded": loaded,
        "cached": cached,
    }


def unload_models() -> None:
    """Drop cached Kokoro handles + ORT sessions + style vectors. The
    native arenas release on garbage-collection of the underlying
    objects."""
    _kokoro_cache.clear()
    _style_cache.clear()


async def warm_up(cfg: KokoroTTSConfig) -> None:
    """Load the configured model + blend and run a tiny synthesis to
    warm the ORT session.

    Mirror startup invokes this so the first spoken sentence doesn't pay
    the model-init latency. Failures bubble up so the engine can latch a
    `disabled_reason` and fall back to the next provider in the chain."""
    entry = await asyncio.to_thread(_load_kokoro, cfg)
    style = await asyncio.to_thread(_resolve_style, entry["kokoro"], cfg.mix)
    preset = _resolve_preset(cfg, _DEFAULT_PRESET)
    await asyncio.to_thread(
        _synthesize_blocking, entry["kokoro"], "ok", style, preset, cfg.lang, cfg.sample_rate
    )


def _resolve_preset(cfg: KokoroTTSConfig, preset_key: str | None) -> KokoroPreset:
    """Pick a synthesis preset. Falls back to `answer` then to safe
    defaults so a missing-preset YAML edit can't kill synthesis mid-turn."""
    if preset_key and preset_key in cfg.presets:
        return cfg.presets[preset_key]
    if _DEFAULT_PRESET in cfg.presets:
        return cfg.presets[_DEFAULT_PRESET]
    if cfg.presets:
        return next(iter(cfg.presets.values()))
    return KokoroPreset(speed=1.0, sentence_silence=0.2)


def _load_kokoro(cfg: KokoroTTSConfig) -> dict[str, Any]:
    """One Kokoro+session per (model_path, voices_path, device). Module-
    level cache survives across calls so the ONNX init only runs once."""
    if not cfg.model_path.exists():
        raise KokoroTTSError(
            f"Kokoro model not found at {cfg.model_path}. Download "
            "kokoro-v1.0.onnx from "
            "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx "
            f"and place it in {cfg.model_path.parent}/ — see "
            "tesseract/voice/models/kokoro/README.md"
        )
    if not cfg.voices_path.exists():
        raise KokoroTTSError(
            f"Kokoro voices bundle not found at {cfg.voices_path}. "
            "Download voices-v1.0.bin from the same release."
        )

    key = (
        str(cfg.model_path.resolve()),
        str(cfg.voices_path.resolve()),
        cfg.device,
    )
    cached = _kokoro_cache.get(key)
    if cached is not None:
        return cached

    if cfg.device == "cuda":
        _register_cuda_dlls()

    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - optional dep
        raise KokoroTTSError(
            "onnxruntime not installed — `pip install -e tesseract[voice-local]` "
            "(see tesseract/pyproject.toml)"
        ) from exc
    try:
        from kokoro_onnx import Kokoro  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - optional dep
        raise KokoroTTSError(
            "kokoro-onnx not installed — `pip install -e tesseract[voice-local]`"
        ) from exc

    available = ort.get_available_providers()
    if cfg.device == "cuda":
        preferred = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    else:
        preferred = ["CPUExecutionProvider"]
    providers = [p for p in preferred if p in available] or ["CPUExecutionProvider"]

    try:
        sess = _session_from_cached_graph(ort, cfg.model_path, providers)
    except Exception as exc:
        raise KokoroTTSError(f"onnxruntime InferenceSession failed: {exc}") from exc

    active = sess.get_providers()[0]
    if cfg.device == "cuda" and active != "CUDAExecutionProvider":
        logger.warning(
            "Kokoro: requested device=cuda but onnxruntime resolved to %s — "
            "check CUDA / cuDNN DLL availability",
            active,
        )

    try:
        kokoro = Kokoro.from_session(sess, str(cfg.voices_path))
    except Exception as exc:
        raise KokoroTTSError(f"Kokoro.from_session failed: {exc}") from exc

    entry = {"kokoro": kokoro, "session": sess, "active_provider": active}
    _kokoro_cache[key] = entry
    logger.info(
        "Kokoro: loaded model=%s provider=%s voices=%d",
        cfg.model_path.name,
        active,
        len(kokoro.get_voices()),
    )
    return entry


def _session_from_cached_graph(ort: Any, model_path: Path, providers: list[str]) -> Any:
    """Build the session from a pre-optimised graph, writing it on first use.

    Most of an `InferenceSession` construction is graph optimisation, and it
    is redone from scratch on every launch. That would be merely wasteful if
    it happened off the event loop, but onnxruntime's constructor holds the
    GIL, so every second of it is a second the backend answers nothing.

    Measured on this catalog's Kokoro model: 4.26s to optimise-and-build,
    1.96s to build from the cached graph. Lowering the optimisation level
    instead reaches a similar build time but pays it back at every synthesis;
    caching keeps the fully optimised graph and pays nothing.

    The cache is derived, never authoritative: any failure to write it, and
    any failure to load it, falls back to building from the original model.

    It lives under `runtime/` and NOT beside the model. Beside the model is
    inside the app tree, which the updater COPIES wholesale to swap versions —
    a 325 MB derived file there would be duplicated on every update, making
    worse the exact update-copy cost the plan already tracks. `runtime/` is
    machine-local, never synced between the operator's machines, and correct
    for something regenerable that describes this box's onnxruntime build.

    Keyed by the model's name and mtime, so a re-fetched or swapped model
    cannot silently keep using a graph optimised from the previous one.
    """
    from tesseract.paths import runtime_dir

    try:
        cache_dir = runtime_dir() / "onnx-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / f"{model_path.stem}.optimized.onnx"
    except OSError as exc:
        logger.debug("Kokoro: no onnx cache dir (%s) — building from source", exc)
        return ort.InferenceSession(str(model_path), providers=providers)
    try:
        fresh = cached.is_file() and cached.stat().st_mtime >= model_path.stat().st_mtime
    except OSError:
        fresh = False

    if fresh:
        options = ort.SessionOptions()
        # The graph on disk is already optimised; re-optimising it is the
        # cost this exists to avoid.
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        try:
            return ort.InferenceSession(str(cached), options, providers=providers)
        except Exception as exc:  # noqa: BLE001 — a stale/corrupt cache is recoverable
            logger.warning("Kokoro: cached graph unusable (%s) — rebuilding", exc)

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    try:
        options.optimized_model_filepath = str(cached)
    except Exception:  # noqa: BLE001 — writing the cache is best-effort
        logger.debug("Kokoro: cannot stage optimised graph at %s", cached)
    try:
        return ort.InferenceSession(str(model_path), options, providers=providers)
    except Exception:
        # A read-only or full model dir fails the WRITE, not the build, so
        # retry once without asking for the cache before giving up.
        logger.warning("Kokoro: could not write optimised graph — building without cache")
        plain = ort.SessionOptions()
        plain.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(str(model_path), plain, providers=providers)


def _resolve_style(kokoro: Any, mix: Mapping[str, float]) -> Any:
    """Build (or fetch from cache) the blended style embedding for the
    given mix. The cache key is a sorted tuple so weight order doesn't
    matter."""
    if not mix:
        raise KokoroTTSError("Kokoro mix is empty — at least one voice required")

    sig = tuple(sorted((vid, float(w)) for vid, w in mix.items()))
    cached = _style_cache.get(sig)
    if cached is not None:
        return cached

    available = set(kokoro.get_voices())
    missing = [vid for vid in mix if vid not in available]
    if missing:
        raise KokoroTTSError(f"Kokoro: unknown voice id(s) in mix: {missing}")

    style = None
    for vid, weight in mix.items():
        contribution = kokoro.get_voice_style(vid) * float(weight)
        style = contribution if style is None else style + contribution
    _style_cache[sig] = style
    return style


def _sanitize_for_tts(text: str) -> str:
    """Strip bracketed style cues (`[whispers]`) and markdown emphasis
    (`*`, `**`) so they don't surface as spoken artefacts."""
    cleaned = _STYLE_CUE_RE.sub("", text)
    cleaned = _MARKDOWN_EMPHASIS_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _synthesize_blocking(
    kokoro: Any,
    text: str,
    style: Any,
    preset: KokoroPreset,
    lang: str,
    sample_rate: int,
) -> bytes:
    """Synchronous synth path — owned by `asyncio.to_thread` so it can't
    stall the event loop. Returns a fully-wrapped WAV (int16 PCM)."""
    import numpy as np  # numpy is a hard dep of the runtime

    try:
        samples, sr = kokoro.create(text, voice=style, speed=preset.speed, lang=lang)
    except Exception as exc:
        raise KokoroTTSError(f"Kokoro synth failed: {exc}") from exc

    if sr != sample_rate:
        logger.warning(
            "Kokoro: model returned sr=%d but config expects %d — using model rate",
            sr, sample_rate,
        )
        sample_rate = sr

    pcm_int16 = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16).tobytes()

    silence_samples = int(preset.sentence_silence * sample_rate)
    if silence_samples > 0:
        pcm_int16 = pcm_int16 + (b"\x00\x00" * silence_samples)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_int16)
    return buf.getvalue()


async def synthesize(
    text: str,
    cfg: KokoroTTSConfig,
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

    key = (str(cfg.model_path.resolve()), str(cfg.voices_path.resolve()), cfg.device)
    entry = _kokoro_cache.get(key)
    if entry is None:
        entry = await asyncio.to_thread(_load_kokoro, cfg)
    kokoro = entry["kokoro"]
    style = _style_cache.get(tuple(sorted((v, float(w)) for v, w in cfg.mix.items())))
    if style is None:
        style = await asyncio.to_thread(_resolve_style, kokoro, cfg.mix)
    chosen = _resolve_preset(cfg, preset)
    return await asyncio.to_thread(
        _synthesize_blocking, kokoro, spoken_text, style, chosen, cfg.lang, cfg.sample_rate
    )
