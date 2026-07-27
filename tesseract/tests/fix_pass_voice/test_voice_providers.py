"""Voice provider unit tests — Gemini Flash audio (STT) + Gemini Flash
TTS. Each provider is exercised against a stubbed `genai.Client` so no
network is required to run the suite.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tesseract.voice.providers import gemini as gemini_provider
from tesseract.voice.providers import gemini_tts as gemini_tts_provider


# ─── Gemini Flash audio STT ────────────────────────────────


@pytest.fixture
def gemini_cfg():
    return gemini_provider.GeminiSTTConfig(
        model="gemini-2.5-flash",
        api_key_env="GOOGLE_API_KEY",
        prompt="Transcribe.",
        timeout_seconds=30.0,
    )


@pytest.fixture(autouse=True)
def _reset_gemini_client_caches():
    """Drop any cached client between tests so factory injections take."""
    gemini_provider.set_client_factory(None)
    gemini_tts_provider.set_client_factory(None)
    yield
    gemini_provider.set_client_factory(None)
    gemini_tts_provider.set_client_factory(None)


def _fake_genai_client(text: str | None, raise_exc: Exception | None = None):
    """Return a stub object shaped like `genai.Client` such that
    `client.aio.models.generate_content(...)` returns an object with `.text`."""
    async def fake_generate(model, contents, config=None):
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(text=text, candidates=[])
    return SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=fake_generate),
        ),
    )


async def test_gemini_transcribe_returns_text(gemini_cfg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    gemini_provider.set_client_factory(lambda key: _fake_genai_client("hello world"))

    out = await gemini_provider.transcribe(b"\x00" * 32_000, gemini_cfg)
    assert out == "hello world"


async def test_gemini_transcribe_strips_whitespace(gemini_cfg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    gemini_provider.set_client_factory(lambda key: _fake_genai_client("  hi  "))

    out = await gemini_provider.transcribe(b"\x00" * 1024, gemini_cfg)
    assert out == "hi"


async def test_gemini_transcribe_returns_empty_on_no_audio(gemini_cfg, monkeypatch):
    """Empty input must short-circuit before the SDK is touched."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    sdk_called = False

    def boom_factory(key):
        nonlocal sdk_called
        sdk_called = True
        return _fake_genai_client("should-not-be-called")

    gemini_provider.set_client_factory(boom_factory)
    out = await gemini_provider.transcribe(b"", gemini_cfg)
    assert out == ""
    assert sdk_called is False


async def test_gemini_transcribe_missing_key_raises(gemini_cfg, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(gemini_provider.GeminiSTTError, match="GOOGLE_API_KEY"):
        await gemini_provider.transcribe(b"\x00" * 1024, gemini_cfg)


async def test_gemini_transcribe_wraps_provider_exceptions(gemini_cfg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    gemini_provider.set_client_factory(
        lambda key: _fake_genai_client(None, raise_exc=RuntimeError("transport boom"))
    )
    with pytest.raises(gemini_provider.GeminiSTTError, match="Gemini STT call failed"):
        await gemini_provider.transcribe(b"\x00" * 1024, gemini_cfg)


async def test_gemini_transcribe_falls_back_to_candidate_text(gemini_cfg, monkeypatch):
    """Defensive: when the SDK omits `.text`, the provider walks
    `candidates[0].content.parts[*].text` instead of returning empty."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text="from-candidate")]),
    )

    async def fake_generate(model, contents, config=None):
        return SimpleNamespace(text=None, candidates=[candidate])

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=fake_generate),
        ),
    )
    gemini_provider.set_client_factory(lambda key: fake_client)

    out = await gemini_provider.transcribe(b"\x00" * 1024, gemini_cfg)
    assert out == "from-candidate"


def test_gemini_wrap_pcm_emits_riff_header():
    pcm = b"\x00\x01" * 4
    out = gemini_provider._wrap_pcm_in_wav(pcm)
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WAVE"


def test_gemini_is_wav_detects_riff_envelope():
    pcm = b"\x00" * 1024
    wav = gemini_provider._wrap_pcm_in_wav(pcm)
    assert gemini_provider._is_wav(wav) is True
    assert gemini_provider._is_wav(pcm) is False


# ─── Gemini Flash TTS ──────────────────────────────────────


@pytest.fixture
def gemini_tts_cfg():
    return gemini_tts_provider.GeminiTTSConfig(
        model="gemini-2.5-flash-preview-tts",
        api_key_env="GOOGLE_API_KEY",
        voice_id="Charon",
        timeout_seconds=30.0,
    )


def _fake_tts_client(pcm: bytes, raise_exc: Exception | None = None):
    """Stub `genai.Client` whose `generate_content` returns a response
    carrying `pcm` bytes on `candidates[0].content.parts[0].inline_data.data`."""
    captured: dict = {}

    async def fake_generate(model, contents, config=None):
        captured["model"] = model
        captured["contents"] = contents
        captured["config"] = config
        if raise_exc is not None:
            raise raise_exc
        part = SimpleNamespace(inline_data=SimpleNamespace(data=pcm))
        candidate = SimpleNamespace(content=SimpleNamespace(parts=[part]))
        return SimpleNamespace(candidates=[candidate])

    client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=fake_generate),
        ),
    )
    return client, captured


async def test_gemini_tts_synthesize_returns_wav(gemini_tts_cfg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    pcm = b"\x00\x01" * 1200  # 100 ms of 24 kHz/16-bit/mono
    client, _captured = _fake_tts_client(pcm)
    gemini_tts_provider.set_client_factory(lambda key: client)

    out = await gemini_tts_provider.synthesize("hello", gemini_tts_cfg)
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WAVE"
    assert pcm in out  # PCM is appended after the header


async def test_gemini_tts_uses_director_notes_pattern_for_preset(monkeypatch):
    """Per-surface preset selects a `style_prompt`; the contents string
    follows the documented Gemini-TTS Director's-Notes pattern
    `"<style_prompt>: <transcript>"`. Gemini interprets the prefix as
    instruction and does NOT speak it."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    cfg = gemini_tts_provider.GeminiTTSConfig(
        model="gemini-2.5-flash-preview-tts",
        api_key_env="GOOGLE_API_KEY",
        voice_id="Charon",
        timeout_seconds=30.0,
        presets={
            "intent": gemini_tts_provider.GeminiPreset(style_prompt="Read briskly"),
            "answer": gemini_tts_provider.GeminiPreset(style_prompt="Read calmly"),
        },
    )
    pcm = b"\x00\x01" * 1200
    client, captured = _fake_tts_client(pcm)
    gemini_tts_provider.set_client_factory(lambda key: client)

    await gemini_tts_provider.synthesize("hello", cfg, preset="intent")
    assert captured["contents"] == "Read briskly: hello"

    await gemini_tts_provider.synthesize("hello", cfg, preset="answer")
    assert captured["contents"] == "Read calmly: hello"


async def test_gemini_tts_unknown_preset_falls_back_to_answer(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    cfg = gemini_tts_provider.GeminiTTSConfig(
        model="gemini-2.5-flash-preview-tts",
        api_key_env="GOOGLE_API_KEY",
        voice_id="Charon",
        timeout_seconds=30.0,
        presets={
            "answer": gemini_tts_provider.GeminiPreset(style_prompt="Calm"),
        },
    )
    pcm = b"\x00\x01" * 1200
    client, captured = _fake_tts_client(pcm)
    gemini_tts_provider.set_client_factory(lambda key: client)

    await gemini_tts_provider.synthesize("hi", cfg, preset="nope")
    assert captured["contents"] == "Calm: hi"


async def test_gemini_tts_no_presets_sends_transcript_only(gemini_tts_cfg, monkeypatch):
    """Empty `presets` map → `contents = transcript`. No style prefix,
    no spurious colon, no leaked instruction."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    pcm = b"\x00\x01" * 1200
    client, captured = _fake_tts_client(pcm)
    gemini_tts_provider.set_client_factory(lambda key: client)

    await gemini_tts_provider.synthesize("hello", gemini_tts_cfg)
    assert captured["contents"] == "hello"


async def test_gemini_tts_strips_bracket_style_cues(gemini_tts_cfg, monkeypatch):
    """`[laughs]`, `[whispers]`, etc. are first-class Gemini TTS style
    cues — leaving them in operator text causes mid-sentence tone shifts.
    Sanitiser strips them before synth."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    pcm = b"\x00\x01" * 1200
    client, captured = _fake_tts_client(pcm)
    gemini_tts_provider.set_client_factory(lambda key: client)

    await gemini_tts_provider.synthesize(
        "Hello [whispers] world", gemini_tts_cfg,
    )
    assert captured["contents"] == "Hello world"


async def test_gemini_tts_strips_markdown_emphasis(gemini_tts_cfg, monkeypatch):
    """Markdown `*emphasis*` triggers Gemini TTS prosody changes. Strip
    asterisks so plain text reads in a level voice."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    pcm = b"\x00\x01" * 1200
    client, captured = _fake_tts_client(pcm)
    gemini_tts_provider.set_client_factory(lambda key: client)

    await gemini_tts_provider.synthesize(
        "Hello **bold** and *italic* text", gemini_tts_cfg,
    )
    assert captured["contents"] == "Hello bold and italic text"


async def test_gemini_tts_voice_id_override_threads_to_speech_config(
    gemini_tts_cfg, monkeypatch,
):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    pcm = b"\x00\x01" * 1200
    client, captured = _fake_tts_client(pcm)
    gemini_tts_provider.set_client_factory(lambda key: client)

    await gemini_tts_provider.synthesize(
        "hi", gemini_tts_cfg, voice_id="Algieba",
    )
    cfg = captured["config"]
    assert "AUDIO" in (cfg.response_modalities or [])
    voice_name = cfg.speech_config.voice_config.prebuilt_voice_config.voice_name
    assert voice_name == "Algieba"


async def test_gemini_tts_missing_key_raises(gemini_tts_cfg, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(gemini_tts_provider.GeminiTTSError, match="GOOGLE_API_KEY"):
        await gemini_tts_provider.synthesize("hi", gemini_tts_cfg)


async def test_gemini_tts_empty_text_returns_empty(gemini_tts_cfg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    sdk_called = False

    def boom_factory(key):
        nonlocal sdk_called
        sdk_called = True
        return _fake_tts_client(b"\x00")[0]

    gemini_tts_provider.set_client_factory(boom_factory)
    out = await gemini_tts_provider.synthesize("   ", gemini_tts_cfg)
    assert out == b""
    assert sdk_called is False


async def test_gemini_tts_wraps_provider_exceptions(gemini_tts_cfg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    client, _captured = _fake_tts_client(b"", raise_exc=RuntimeError("transport boom"))
    gemini_tts_provider.set_client_factory(lambda key: client)

    with pytest.raises(gemini_tts_provider.GeminiTTSError, match="Gemini TTS call failed"):
        await gemini_tts_provider.synthesize("hi", gemini_tts_cfg)


async def test_gemini_tts_missing_audio_payload_raises(gemini_tts_cfg, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    async def fake_generate(model, contents, config=None):
        return SimpleNamespace(candidates=[])

    client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=fake_generate),
        ),
    )
    gemini_tts_provider.set_client_factory(lambda key: client)

    with pytest.raises(gemini_tts_provider.GeminiTTSError, match="missing audio"):
        await gemini_tts_provider.synthesize("hi", gemini_tts_cfg)
