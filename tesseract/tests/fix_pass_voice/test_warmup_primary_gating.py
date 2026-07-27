"""Voice-runtime boot warmup gates on role-primary status.

After 2026-05-14 operator made `piper` TTS primary. The prior behaviour
fired `warm_up_kokoro` / `warm_up_piper` whenever the catalog entry's
`preload` flag was truthy regardless of chain position — so a fallback
local engine paid full CUDA load at boot for a service that would never
be reached on the happy path. The new rule: warm only when this engine
is `chain[0]`, or when the operator explicitly opts in via catalog
`preload: true` (escape hatch for fallback-preload loadouts).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from tesseract.mirror.server import app as app_module


def _make_app() -> web.Application:
    a = web.Application()
    a["_warmup_tasks"] = []
    a["cost_ledger"] = None
    return a


def _stt_chain_primary_whisper() -> list[dict[str, Any]]:
    return [
        {
            "adapter": "local_whisper",
            "provider": "local_whisper",
            "model": "large-v3-turbo",
            "device": "cuda",
            "compute_type": "int8_float16",
            "preload": True,
            "ref": "local.whisper.local_whisper",
        },
    ]


def _stt_chain_primary_cloud() -> list[dict[str, Any]]:
    return [
        {
            "adapter": "gemini",
            "provider": "gemini_flash_audio",
            "model": "gemini-2.5-flash",
            "api_key_env": "GOOGLE_API_KEY",
            "prompt": "Transcribe.",
            "timeout_seconds": 20,
            "ref": "api.google.gemini_flash_audio",
        },
        {
            "adapter": "local_whisper",
            "provider": "local_whisper",
            "model": "large-v3-turbo",
            "device": "cuda",
            "compute_type": "int8_float16",
            "preload": False,
            "ref": "local.whisper.local_whisper",
        },
    ]


def _piper_entry(*, preload: bool = False) -> dict[str, Any]:
    return {
        "adapter": "piper",
        "provider": "piper_northern_english_male",
        "model": "en_GB-northern_english_male-medium.onnx",
        "sample_rate": 22050,
        "preload": preload,
        "ref": "local.piper.northern_english_male",
    }


def _kokoro_entry(*, preload: bool = False) -> dict[str, Any]:
    return {
        "adapter": "kokoro",
        "provider": "kokoro_charon",
        "model": "kokoro-v1.0.onnx",
        "voices_file": "voices-v1.0.bin",
        "sample_rate": 24000,
        "lang": "en-gb",
        "device": "cuda",
        "mix": {"bm_george": 0.4, "bm_daniel": 0.6},
        "preload": preload,
        "timeout_seconds": 60,
        "ref": "local.kokoro.charon",
    }


def _gemini_tts_entry() -> dict[str, Any]:
    return {
        "adapter": "gemini",
        "provider": "gemini_flash_tts",
        "model": "gemini-2.5-flash-preview-tts",
        "api_key_env": "GOOGLE_API_KEY",
        "voice_id": "Charon",
        "timeout_seconds": 20,
        "ref": "api.google.gemini_flash_tts",
    }


@pytest.fixture
def patched_engines():
    fake_stt = MagicMock()
    fake_stt.warm_up_local = MagicMock(side_effect=lambda: asyncio.sleep(0))
    fake_tts = MagicMock()
    fake_tts.warm_up_kokoro = MagicMock(side_effect=lambda: asyncio.sleep(0))
    fake_tts.warm_up_piper = MagicMock(side_effect=lambda: asyncio.sleep(0))

    # Patch the module attributes, not the `from`-imported local names in
    # `_build_voice_runtime`. Production code does `from tesseract.voice
    # import STTEngine` and `from tesseract.brain.boot import
    # load_voice_config` inside the function — both resolve module
    # attributes at call time, so module-path patches take effect. If
    # those imports are ever hoisted to module level, the patch targets
    # become `tesseract.mirror.server.app.STTEngine` / `…load_voice_config`.
    with patch("tesseract.voice.STTEngine", return_value=fake_stt), patch(
        "tesseract.voice.TTSEngine", return_value=fake_tts
    ), patch("tesseract.brain.boot.load_voice_config") as load_cfg:
        yield fake_stt, fake_tts, load_cfg


async def _run(
    cfg: dict[str, Any], patched: tuple
) -> tuple[web.Application, MagicMock, MagicMock]:
    fake_stt, fake_tts, load_cfg = patched
    load_cfg.return_value = cfg
    a = _make_app()
    app_module._build_voice_runtime(a)
    return a, fake_stt, fake_tts


async def _drain(a: web.Application) -> None:
    pending = [t for t in a.get("_warmup_tasks", []) if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_whisper_warms_when_primary_stt(patched_engines):
    cfg = {
        "stt": {"chain": _stt_chain_primary_whisper()},
        "tts": {"chain": [_piper_entry(preload=False)]},
    }
    a, fake_stt, _ = await _run(cfg, patched_engines)

    names = [t.get_name() for t in a["_warmup_tasks"]]
    assert any(n == "warmup:whisper" for n in names)
    fake_stt.warm_up_local.assert_called_once()
    await _drain(a)


async def test_whisper_does_not_warm_when_fallback_without_preload(patched_engines):
    cfg = {
        "stt": {"chain": _stt_chain_primary_cloud()},
        "tts": {"chain": [_piper_entry()]},
    }
    a, fake_stt, _ = await _run(cfg, patched_engines)

    names = [t.get_name() for t in a["_warmup_tasks"]]
    assert not any(n == "warmup:whisper" for n in names)
    fake_stt.warm_up_local.assert_not_called()
    await _drain(a)


async def test_whisper_fallback_with_explicit_preload_warms(patched_engines):
    chain = _stt_chain_primary_cloud()
    chain[1]["preload"] = True
    cfg = {
        "stt": {"chain": chain},
        "tts": {"chain": [_piper_entry()]},
    }
    a, fake_stt, _ = await _run(cfg, patched_engines)

    names = [t.get_name() for t in a["_warmup_tasks"]]
    assert any(n == "warmup:whisper" for n in names)
    fake_stt.warm_up_local.assert_called_once()
    await _drain(a)


async def test_piper_warms_when_primary_tts(patched_engines):
    cfg = {
        "stt": {"chain": _stt_chain_primary_cloud()},
        "tts": {"chain": [_piper_entry(), _gemini_tts_entry(), _kokoro_entry()]},
    }
    a, _, fake_tts = await _run(cfg, patched_engines)

    names = [t.get_name() for t in a["_warmup_tasks"]]
    assert any(n == "warmup:piper" for n in names)
    assert not any(n == "warmup:kokoro" for n in names)
    fake_tts.warm_up_piper.assert_called_once()
    fake_tts.warm_up_kokoro.assert_not_called()
    await _drain(a)


async def test_piper_does_not_warm_when_fallback(patched_engines):
    cfg = {
        "stt": {"chain": _stt_chain_primary_cloud()},
        "tts": {"chain": [_gemini_tts_entry(), _piper_entry(), _kokoro_entry()]},
    }
    a, _, fake_tts = await _run(cfg, patched_engines)

    names = [t.get_name() for t in a["_warmup_tasks"]]
    assert not any(n == "warmup:piper" for n in names)
    assert not any(n == "warmup:kokoro" for n in names)
    fake_tts.warm_up_piper.assert_not_called()
    fake_tts.warm_up_kokoro.assert_not_called()
    await _drain(a)


async def test_kokoro_warms_when_primary_tts(patched_engines):
    cfg = {
        "stt": {"chain": _stt_chain_primary_cloud()},
        "tts": {"chain": [_kokoro_entry(), _piper_entry(), _gemini_tts_entry()]},
    }
    a, _, fake_tts = await _run(cfg, patched_engines)

    names = [t.get_name() for t in a["_warmup_tasks"]]
    assert any(n == "warmup:kokoro" for n in names)
    assert not any(n == "warmup:piper" for n in names)
    fake_tts.warm_up_kokoro.assert_called_once()
    fake_tts.warm_up_piper.assert_not_called()
    await _drain(a)


async def test_kokoro_fallback_with_explicit_preload_warms(patched_engines):
    cfg = {
        "stt": {"chain": _stt_chain_primary_cloud()},
        "tts": {
            "chain": [
                _piper_entry(),
                _kokoro_entry(preload=True),
                _gemini_tts_entry(),
            ]
        },
    }
    a, _, fake_tts = await _run(cfg, patched_engines)

    names = [t.get_name() for t in a["_warmup_tasks"]]
    assert any(n == "warmup:kokoro" for n in names)
    fake_tts.warm_up_kokoro.assert_called_once()
    await _drain(a)


async def test_cloud_only_tts_no_local_warm(patched_engines):
    cfg = {
        "stt": {"chain": _stt_chain_primary_cloud()},
        "tts": {"chain": [_gemini_tts_entry()]},
    }
    a, _, fake_tts = await _run(cfg, patched_engines)

    names = [t.get_name() for t in a["_warmup_tasks"]]
    assert not any(n.startswith("warmup:piper") for n in names)
    assert not any(n.startswith("warmup:kokoro") for n in names)
    fake_tts.warm_up_piper.assert_not_called()
    fake_tts.warm_up_kokoro.assert_not_called()
    await _drain(a)
