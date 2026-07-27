"""Mirror voice REST surface (`/api/voice/*`) — post-G2 shape.

Both lanes are cloud-only Gemini after 2026-04-26; the route reports a
single mode per lane and `current_voice` carries `voice_id` only
(per-surface tone is config-only via roles.yaml `synthesis_presets`)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import voice as voice_route


def _make_app(*, voice_cfg=None, tts_engine=None, voice_state=None) -> web.Application:
    app = web.Application()
    config = MagicMock()
    config.models = {"voice": voice_cfg} if voice_cfg is not None else {}
    app["config"] = config
    app["voice_state"] = voice_state
    app["tts_engine"] = tts_engine
    app.router.add_get("/api/voice/providers", voice_route.get_providers)
    app.router.add_post("/api/voice/test", voice_route.post_test)
    return app


@pytest.fixture
async def client():
    clients = []

    async def _factory(**kwargs):
        app = _make_app(**kwargs)
        server = TestServer(app)
        c = TestClient(server)
        await c.start_server()
        clients.append(c)
        return c

    try:
        yield _factory
    finally:
        for c in clients:
            await c.close()


async def test_providers_returns_disabled_when_no_voice_block(client):
    c = await client()
    r = await c.get("/api/voice/providers")
    assert r.status == 200
    data = await r.json()
    assert data == {"enabled": False, "reason": "no `voice:` block in roles.yaml"}


async def test_providers_returns_full_shape_when_configured(client):
    """Chain-shaped voice config — `mode` + `chain[]` per lane, with
    `primary` and `fallbacks[]` summarized for the Settings picker."""
    voice_cfg = {
        "stt": {
            "mode": "active",
            "chain": [
                {
                    "ref": "local.whisper.local_whisper",
                    "adapter": "local_whisper",
                    "provider": "whisper",
                    "model": "large-v3-turbo",
                    "daily_budget_usd": 0.0,
                },
                {
                    "ref": "api.google.gemini_flash_audio",
                    "adapter": "gemini",
                    "provider": "google",
                    "model": "gemini-2.5-flash",
                    "daily_budget_usd": 1.0,
                },
            ],
        },
        "tts": {
            "mode": "active",
            "chain": [
                {
                    "ref": "api.google.gemini_flash_tts",
                    "adapter": "gemini",
                    "provider": "google",
                    "model": "gemini-2.5-flash-preview-tts",
                    "daily_budget_usd": 1.0,
                },
            ],
        },
        "default_voice_id": "Charon",
    }
    voice_state = MagicMock()
    voice_state.voice_id = "Charon"
    c = await client(voice_cfg=voice_cfg, voice_state=voice_state)
    r = await c.get("/api/voice/providers")
    assert r.status == 200
    data = await r.json()
    assert data["enabled"] is True
    assert data["stt"]["mode"] == "active"
    assert data["stt"]["primary"] == "local.whisper.local_whisper"
    assert data["stt"]["fallbacks"] == ["api.google.gemini_flash_audio"]
    assert len(data["stt"]["chain"]) == 2
    assert data["tts"]["mode"] == "active"
    assert data["tts"]["primary"] == "api.google.gemini_flash_tts"
    assert data["tts"]["fallbacks"] == []
    assert data["default_voice_id"] == "Charon"
    assert data["current_voice"]["voice_id"] == "Charon"
    assert "tone_prompt" not in data["current_voice"]


async def test_post_test_503_when_engine_unavailable(client):
    c = await client()
    r = await c.post("/api/voice/test", json={"text": "hello"})
    assert r.status == 503
    data = await r.json()
    assert data["error"] == "tts_engine_unavailable"


def _fake_engine(synthesize_return=None, synthesize_exc=None):
    engine = MagicMock()
    if synthesize_exc is not None:
        engine.synthesize = AsyncMock(side_effect=synthesize_exc)
    else:
        engine.synthesize = AsyncMock(return_value=synthesize_return)
    return engine


async def test_post_test_returns_audio_b64(client):
    engine = _fake_engine(synthesize_return=(b"\x00\x01\x02", "gemini_flash_tts"))
    voice_state = MagicMock()
    voice_state.voice_id = "Charon"
    c = await client(tts_engine=engine, voice_state=voice_state)
    r = await c.post("/api/voice/test", json={"text": "hello world"})
    assert r.status == 200
    data = await r.json()
    assert data["provider"] == "gemini_flash_tts"
    assert data["byte_count"] == 3
    assert data["char_count"] == 11
    # base64 of b"\x00\x01\x02" is "AAEC"
    assert data["audio_b64"] == "AAEC"
    engine.synthesize.assert_awaited_once()
    args, _kwargs = engine.synthesize.await_args
    assert args[0] == "hello world"
    assert args[1].voice_id == "Charon"


async def test_post_test_overrides_voice_id(client):
    """voice_id can be overridden per-call. Tone is config-only and
    not accepted in the request body."""
    engine = _fake_engine(synthesize_return=(b"x", "gemini_flash_tts"))
    voice_state = MagicMock()
    voice_state.voice_id = "Charon"
    c = await client(tts_engine=engine, voice_state=voice_state)
    r = await c.post(
        "/api/voice/test",
        json={"text": "hi", "voice_id": "Algieba"},
    )
    assert r.status == 200
    args, _kwargs = engine.synthesize.await_args
    assert args[1].voice_id == "Algieba"


async def test_post_test_rejects_oversized_text(client):
    engine = _fake_engine(synthesize_return=(b"", ""))
    c = await client(tts_engine=engine)
    r = await c.post("/api/voice/test", json={"text": "x" * 1000})
    assert r.status == 400
    data = await r.json()
    assert "exceeds" in data["error"]


async def test_post_test_502_on_engine_failure(client):
    engine = _fake_engine(synthesize_exc=RuntimeError("boom"))
    c = await client(tts_engine=engine)
    r = await c.post("/api/voice/test", json={"text": "hi"})
    assert r.status == 502
    data = await r.json()
    assert data["error"] == "synthesis_failed"
    assert "boom" in data["reason"]
