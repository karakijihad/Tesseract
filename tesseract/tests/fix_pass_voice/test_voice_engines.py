"""STTEngine + TTSEngine — orchestration of provider selection and
ledger debits. Each test patches the provider modules so no real HTTP
or SDK calls are exercised.

Both lanes are cloud-only Gemini after the G2 cutover (2026-04-26).
Hitting either provider's daily cap raises `BudgetExhausted`; there is
no fallback target on either side.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tesseract.brain.cost import BudgetExhausted, TtsUsage
from tesseract.voice import STTEngine, TTSEngine, VoiceParams
from tesseract.voice.providers import gemini as gemini_provider
from tesseract.voice.providers import gemini_tts as gemini_tts_provider
from tesseract.voice.providers import local_whisper


def _stt_engine(voice_ledger):
    return STTEngine(
        cloud_config=gemini_provider.GeminiSTTConfig(
            model="gemini-2.5-flash",
            api_key_env="GOOGLE_API_KEY",
            prompt="Transcribe.",
            timeout_seconds=30.0,
        ),
        cost_ledger=voice_ledger,
    )


def _stt_local_engine(voice_ledger):
    return STTEngine(
        cloud_config=gemini_provider.GeminiSTTConfig(
            model="gemini-2.5-flash",
            api_key_env="GOOGLE_API_KEY",
            prompt="Transcribe.",
            timeout_seconds=30.0,
        ),
        local_config=local_whisper.LocalWhisperConfig(
            provider="local_whisper",
            model="large-v3-turbo",
            device="cuda",
            compute_type="int8_float16",
            language=None,
            beam_size=1,
        ),
        cost_ledger=voice_ledger,
    )


def _tts_engine(voice_ledger):
    return TTSEngine(
        cloud_config=gemini_tts_provider.GeminiTTSConfig(
            model="gemini-2.5-flash-preview-tts",
            api_key_env="GOOGLE_API_KEY",
            voice_id="Charon",
            timeout_seconds=30.0,
        ),
        cost_ledger=voice_ledger,
    )


# ─── TTS ───────────────────────────────────────────────────


async def test_tts_synthesize_records_cost(voice_ledger):
    engine = _tts_engine(voice_ledger)

    async def fake_cloud(text, cfg, **kwargs):
        return b"AUDIO"

    with patch.object(gemini_tts_provider, "synthesize", side_effect=fake_cloud):
        out, provider = await engine.synthesize(
            "hello", VoiceParams(voice_id="Charon", tone_prompt="A British voice."),
        )
    assert out == b"AUDIO"
    assert provider == "gemini_flash_tts"
    assert engine.cost_ledger.voice_provider_total_usd("gemini_flash_tts") > 0


async def test_tts_char_count_is_transcript_length(voice_ledger):
    """Char-count debit covers the transcript only. Per-surface style
    prompts now come from config (synthesis_presets) and are added once
    per call as a small constant — not tracked per-debit."""
    engine = _tts_engine(voice_ledger)

    async def fake_cloud(text, cfg, **kwargs):
        return b"AUDIO"

    text = "hello"
    expected_chars = len(text)

    with patch.object(gemini_tts_provider, "synthesize", side_effect=fake_cloud):
        await engine.synthesize(text, VoiceParams(voice_id="Charon"))
    spent = engine.cost_ledger.voice_provider_total_usd("gemini_flash_tts")
    # cost = (chars / 1_000_000) * 10.00
    assert spent == pytest.approx((expected_chars / 1_000_000) * 10.00, rel=1e-6)


async def test_tts_skips_when_text_empty(voice_ledger):
    engine = _tts_engine(voice_ledger)
    out, provider = await engine.synthesize("   ", VoiceParams(voice_id="Charon"))
    assert out == b""
    assert provider == ""
    # No debit on whitespace-only input.
    assert engine.cost_ledger.voice_provider_total_usd("gemini_flash_tts") == 0.0


async def test_tts_budget_exhaustion_raises_before_call(voice_ledger):
    """Preflight burns the $0.20 TTS cap; the next synthesize must raise
    BudgetExhausted *before* the provider is invoked — no spend leak."""
    engine = _tts_engine(voice_ledger)
    # $0.20 cap / ($10/Mchars) = 20_000 chars.
    voice_ledger.record_voice(
        "tts", "gemini_flash_tts", TtsUsage(char_count=20_000),
    )

    cloud_called = False

    async def fake_cloud(text, cfg, **kwargs):
        nonlocal cloud_called
        cloud_called = True
        return b"AUDIO"

    with patch.object(gemini_tts_provider, "synthesize", side_effect=fake_cloud):
        with pytest.raises(BudgetExhausted):
            await engine.synthesize("hi", VoiceParams(voice_id="Charon"))

    assert cloud_called is False, "preflight must trip before the provider call"


async def test_tts_propagates_provider_errors(voice_ledger):
    """A provider exception surfaces directly — no fallback."""
    engine = _tts_engine(voice_ledger)

    async def fake_cloud(text, cfg, **kwargs):
        raise gemini_tts_provider.GeminiTTSError("simulated outage")

    with patch.object(gemini_tts_provider, "synthesize", side_effect=fake_cloud):
        with pytest.raises(gemini_tts_provider.GeminiTTSError):
            await engine.synthesize("hi", VoiceParams(voice_id="Charon"))


async def test_tts_passes_voice_id_and_preset(voice_ledger):
    """VoiceParams.voice_id and preset must reach the provider so it can
    pick the per-surface style prompt from its synthesis_presets map."""
    engine = _tts_engine(voice_ledger)
    captured: dict = {}

    async def fake_cloud(text, cfg, *, voice_id=None, preset=None):
        captured["voice_id"] = voice_id
        captured["preset"] = preset
        captured["text"] = text
        return b"AUDIO"

    with patch.object(gemini_tts_provider, "synthesize", side_effect=fake_cloud):
        await engine.synthesize(
            "hi",
            VoiceParams(voice_id="Algieba", preset="intent"),
        )
    assert captured == {
        "voice_id": "Algieba",
        "preset": "intent",
        "text": "hi",
    }


# ─── STT (cloud-only Gemini Flash audio) ───────────────────


async def test_stt_cloud_path_records_seconds(monkeypatch, voice_ledger):
    """Happy path: provider returns a transcript, engine yields one final
    pair, ledger records the audio-second debit at gemini_flash_audio."""
    engine = _stt_engine(voice_ledger)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    async def fake_transcribe(audio, cfg):
        assert cfg.model == "gemini-2.5-flash"
        return "hello world"

    monkeypatch.setattr(gemini_provider, "transcribe", fake_transcribe)

    # 32_000 bytes at 16 kHz / 16-bit / mono = 1.0 s of audio.
    chunks = [c async for c in engine.transcribe_stream(b"\x00" * 32_000)]
    assert chunks == [("hello world", True)]

    spent = engine.cost_ledger.voice_provider_total_usd("gemini_flash_audio")
    assert spent > 0
    assert spent == pytest.approx(0.09 / 3600.0, rel=1e-6)


async def test_stt_local_path_wins_without_cloud_billing(monkeypatch, voice_ledger):
    engine = _stt_local_engine(voice_ledger)

    async def fake_local(audio, cfg):
        return "local transcript"

    async def fake_cloud(audio, cfg):
        raise AssertionError("cloud STT should not run when local succeeds")

    monkeypatch.setattr(local_whisper, "transcribe", fake_local)
    monkeypatch.setattr(gemini_provider, "transcribe", fake_cloud)

    chunks = [c async for c in engine.transcribe_stream(b"\x00" * 32_000)]
    assert chunks == [("local transcript", True)]
    assert engine.cost_ledger.voice_provider_total_usd("gemini_flash_audio") == 0.0


async def test_stt_local_failure_falls_back_to_cloud(monkeypatch, voice_ledger):
    engine = _stt_local_engine(voice_ledger)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    async def fake_local(audio, cfg):
        raise local_whisper.LocalWhisperError("model unavailable")

    async def fake_cloud(audio, cfg):
        return "cloud transcript"

    monkeypatch.setattr(local_whisper, "transcribe", fake_local)
    monkeypatch.setattr(gemini_provider, "transcribe", fake_cloud)

    chunks = [c async for c in engine.transcribe_stream(b"\x00" * 32_000)]
    assert chunks == [("cloud transcript", True)]
    assert engine.cost_ledger.voice_provider_total_usd("gemini_flash_audio") > 0


async def test_stt_local_failure_disables_until_reset(monkeypatch, voice_ledger):
    engine = _stt_local_engine(voice_ledger)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    calls = {"local": 0, "cloud": 0}

    async def fake_local(audio, cfg):
        calls["local"] += 1
        raise local_whisper.LocalWhisperError("missing cublas")

    async def fake_cloud(audio, cfg):
        calls["cloud"] += 1
        return f"cloud transcript {calls['cloud']}"

    monkeypatch.setattr(local_whisper, "transcribe", fake_local)
    monkeypatch.setattr(gemini_provider, "transcribe", fake_cloud)

    first = [c async for c in engine.transcribe_stream(b"\x00" * 32_000)]
    second = [c async for c in engine.transcribe_stream(b"\x00" * 32_000)]

    assert first == [("cloud transcript 1", True)]
    assert second == [("cloud transcript 2", True)]
    assert calls == {"local": 1, "cloud": 2}
    status = engine.local_status()
    assert status["disabled"] is True
    assert "missing cublas" in status["disabled_reason"]

    engine.unload_local()
    assert engine.local_status()["disabled"] is False


async def test_stt_local_failure_emits_one_shot_fallback_notice(monkeypatch, voice_ledger):
    """First local→cloud fallback returns a one-shot notice via
    `consume_fallback_notice`; subsequent calls return empty until the
    next latch (i.e. after `unload_local`)."""
    engine = _stt_local_engine(voice_ledger)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    async def fake_local(audio, cfg):
        raise local_whisper.LocalWhisperError("missing cublas")

    async def fake_cloud(audio, cfg):
        return "cloud transcript"

    monkeypatch.setattr(local_whisper, "transcribe", fake_local)
    monkeypatch.setattr(gemini_provider, "transcribe", fake_cloud)

    assert engine.consume_fallback_notice() == ""
    [_ async for _ in engine.transcribe_stream(b"\x00" * 32_000)]
    notice = engine.consume_fallback_notice()
    assert "Local STT disabled" in notice
    assert "missing cublas" in notice
    assert "Gemini" in notice
    # Same latch → no re-notify.
    [_ async for _ in engine.transcribe_stream(b"\x00" * 32_000)]
    assert engine.consume_fallback_notice() == ""


async def test_stt_propagates_provider_errors(monkeypatch, voice_ledger):
    """A Gemini transport / API failure surfaces directly — no fallback."""
    engine = _stt_engine(voice_ledger)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    async def fake_transcribe(audio, cfg):
        raise gemini_provider.GeminiSTTError("simulated 503")

    monkeypatch.setattr(gemini_provider, "transcribe", fake_transcribe)

    with pytest.raises(gemini_provider.GeminiSTTError):
        async for _ in engine.transcribe_stream(b"\x00" * 1024):
            pass


async def test_stt_budget_exhaustion_raises_before_call(monkeypatch, voice_ledger):
    """Preflight burns the $0.30 STT cap; the next transcribe must raise
    BudgetExhausted *before* the provider is invoked — no spend leak."""
    engine = _stt_engine(voice_ledger)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    # Burn through the cap with a large pre-recorded usage. $0.30 cap /
    # ($0.09/hr) = 3.33 audio-hours = 12_000 seconds.
    from tesseract.brain.cost import SttUsage
    voice_ledger.record_voice("stt", "gemini_flash_audio", SttUsage(seconds=12_000.0))

    called = False

    async def fake_transcribe(audio, cfg):
        nonlocal called
        called = True
        return "should not run"

    monkeypatch.setattr(gemini_provider, "transcribe", fake_transcribe)

    with pytest.raises(BudgetExhausted):
        async for _ in engine.transcribe_stream(b"\x00" * 32_000):
            pass
    assert called is False, "preflight must trip before the provider call"
