"""`/api/system/whisper` route shape — must match the
`WhisperStatusResponse` interface in `mirror/src/lib/api.ts`. The
frontend Settings panel calls this on mount + on `Reset Whisper`, so a
field rename here is a UI-side bug."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import local_models as local_models_route
from tesseract.voice import STTEngine
from tesseract.voice.providers import gemini as gemini_provider
from tesseract.voice.providers import local_whisper


_REQUIRED_KEYS = {
    "configured",
    "provider",
    "model",
    "device",
    "compute_type",
    "language",
    "timeout_seconds",
    "disabled",
    "disabled_reason",
    "loaded",
    "cached",
}


def _make_app(stt_engine=None) -> web.Application:
    app = web.Application()
    app["stt_engine"] = stt_engine
    app.router.add_get("/api/system/whisper", local_models_route.whisper_status)
    app.router.add_post("/api/system/whisper", local_models_route.whisper_action)
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


async def test_whisper_status_returns_disabled_shape_when_no_engine(client):
    c = await client()
    r = await c.get("/api/system/whisper")
    assert r.status == 200
    data = await r.json()
    assert set(data.keys()) >= _REQUIRED_KEYS
    assert data["configured"] is False
    assert data["loaded"] is False
    assert data["cached"] == []


async def test_whisper_status_returns_full_shape_when_configured(client):
    engine = STTEngine(
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
            timeout_seconds=20.0,
        ),
        cost_ledger=None,
    )
    c = await client(stt_engine=engine)
    r = await c.get("/api/system/whisper")
    assert r.status == 200
    data = await r.json()
    assert set(data.keys()) >= _REQUIRED_KEYS
    assert data["configured"] is True
    assert data["provider"] == "local_whisper"
    assert data["model"] == "large-v3-turbo"
    assert data["disabled"] is False
    assert data["disabled_reason"] == ""
    assert data["timeout_seconds"] == 20.0


async def test_whisper_status_reflects_disabled_reason(client):
    engine = STTEngine(
        cloud_config=None,
        local_config=local_whisper.LocalWhisperConfig(
            provider="local_whisper",
            model="large-v3-turbo",
            device="cuda",
            compute_type="int8_float16",
            language=None,
            beam_size=1,
            timeout_seconds=20.0,
        ),
        cost_ledger=None,
    )
    engine.local_disabled_reason = "missing cublas64_12.dll"
    c = await client(stt_engine=engine)
    r = await c.get("/api/system/whisper")
    data = await r.json()
    assert data["disabled"] is True
    assert "missing cublas" in data["disabled_reason"]


async def test_whisper_unload_clears_disabled_state(client):
    engine = STTEngine(
        cloud_config=None,
        local_config=local_whisper.LocalWhisperConfig(
            provider="local_whisper",
            model="large-v3-turbo",
            device="cuda",
            compute_type="int8_float16",
            language=None,
            beam_size=1,
            timeout_seconds=20.0,
        ),
        cost_ledger=None,
    )
    engine.local_disabled_reason = "missing cublas64_12.dll"
    c = await client(stt_engine=engine)
    r = await c.post("/api/system/whisper", json={"action": "unload"})
    assert r.status == 200
    assert engine.local_disabled_reason == ""
    assert engine.consume_fallback_notice() == ""


async def test_whisper_action_rejects_unknown_action(client):
    engine = STTEngine(
        cloud_config=None,
        local_config=local_whisper.LocalWhisperConfig(
            provider="local_whisper",
            model="large-v3-turbo",
            device="cuda",
            compute_type="int8_float16",
            language=None,
            beam_size=1,
            timeout_seconds=20.0,
        ),
        cost_ledger=None,
    )
    c = await client(stt_engine=engine)
    r = await c.post("/api/system/whisper", json={"action": "wipe"})
    assert r.status == 400


async def test_whisper_action_503_when_engine_unavailable(client):
    c = await client()
    r = await c.post("/api/system/whisper", json={"action": "unload"})
    assert r.status == 503
