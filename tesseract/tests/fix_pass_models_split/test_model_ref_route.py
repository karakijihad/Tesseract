"""GET /api/settings/catalog + POST /api/settings/model-ref.

Per-role catalog-backed picker: the Settings → Models tab fetches the full
providers.yaml catalog and the current selection per swappable target,
then writes the chosen ref back to roles.yaml (round-trip preserves
operator comments). Targets covered: chat_brain, observer_agent,
agents_default, subagents_default, embeddings, voice_stt, voice_tts.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import settings as settings_route


def _payload_with_voice_stt(baseline_providers: dict, baseline_roles: dict) -> tuple[dict, dict]:
    """Extend the conftest baseline with an STT catalog entry + voice.stt lane
    so the voice_stt swap path is exercisable. Also adds a second TTS lane
    ref so a real swap is testable in voice_tts.
    """
    providers = dict(baseline_providers)
    providers["api"] = dict(providers["api"])
    providers["api"]["google"] = dict(providers["api"]["google"])
    providers["api"]["google"]["models"] = dict(providers["api"]["google"]["models"])
    providers["api"]["google"]["models"]["gemini_flash_audio"] = {
        "kind": "audio_stt",
        "model": "gemini-2.5-flash",
        "cost_per_audio_hour": 0.09,
    }
    providers["api"]["openai"] = dict(providers["api"]["openai"])
    providers["api"]["anthropic"] = {
        "api_key_env": "ANTHROPIC_API_KEY",
        "timeout_seconds": 60,
        "max_retries": 3,
        "adapter": "anthropic",
        "models": {
            "sonnet_46": {
                "model": "claude-sonnet-4-6",
                "context_window": 200000,
                "max_output_tokens": 16384,
                "cost_per_mtok_in": 3.00,
                "cost_per_mtok_out": 15.00,
            },
        },
    }

    roles = dict(baseline_roles)
    roles["roles"] = dict(roles["roles"])
    roles["roles"]["subagents_default"] = {
        "mode": "active",
        "primary": "api.openai.gpt54_nano",
    }
    roles["voice"] = dict(roles["voice"])
    roles["voice"]["stt"] = {
        "mode": "active",
        "primary": "api.google.gemini_flash_audio",
        "settings": {
            "api.google.gemini_flash_audio": {
                "daily_budget_usd": 1.00,
            },
        },
    }
    return providers, roles


async def _make_client(tmp_path: Path, providers: dict, roles: dict) -> TestClient:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "providers.yaml").write_text(yaml.safe_dump(providers), encoding="utf-8")
    (config_dir / "roles.yaml").write_text(yaml.safe_dump(roles), encoding="utf-8")

    app = web.Application()
    app["tesseract_dir"] = tmp_path
    app["config"] = SimpleNamespace(models={})

    app.router.add_get("/api/settings/catalog", settings_route.get_catalog)
    app.router.add_post("/api/settings/model-ref", settings_route.set_model_ref)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


def _roles_doc(tmp_path: Path) -> dict:
    return yaml.safe_load((tmp_path / "config" / "roles.yaml").read_text(encoding="utf-8"))


# ── GET /api/settings/catalog ───────────────────────────────────────


async def test_catalog_lists_entries_and_current(
    tmp_path: Path, baseline_providers: dict, baseline_roles: dict
) -> None:
    providers, roles = _payload_with_voice_stt(baseline_providers, baseline_roles)
    client = await _make_client(tmp_path, providers, roles)
    try:
        r = await client.get("/api/settings/catalog")
        assert r.status == 200
        body = await r.json()

        refs = {e["ref"] for e in body["entries"]}
        assert "api.openai.gpt54_mini" in refs
        assert "api.openai.gpt54_nano" in refs
        assert "api.google.gemini_25_flash" in refs
        assert "api.google.gemini_flash_tts" in refs
        assert "api.google.gemini_flash_audio" in refs
        assert "api.anthropic.sonnet_46" in refs
        assert "cli.claude.opus_47" in refs
        assert "local.ollama.nomic_embed" in refs

        # Each entry carries the right shape.
        gpt = next(e for e in body["entries"] if e["ref"] == "api.openai.gpt54_mini")
        assert gpt["tier"] == "api"
        assert gpt["provider"] == "openai"
        assert gpt["model"] == "gpt-5.4-mini"
        assert gpt["kind"] == "chat"
        assert gpt["context_window"] == 400000

        # Voice/embedding kinds carried verbatim.
        tts = next(e for e in body["entries"] if e["ref"] == "api.google.gemini_flash_tts")
        assert tts["kind"] == "tts"
        emb = next(e for e in body["entries"] if e["ref"] == "local.ollama.nomic_embed")
        assert emb["kind"] == "embedding"

        assert body["current"]["chat_brain"] == "api.openai.gpt54_mini"
        assert body["current"]["observer_agent"] == "api.openai.gpt54_nano"
        assert body["current"]["agents_default"] == "api.openai.gpt54_nano"
        assert body["current"]["subagents_default"] == "api.openai.gpt54_nano"
        assert body["current"]["embeddings"] == "local.ollama.nomic_embed"
        assert body["current"]["voice_stt"] == "api.google.gemini_flash_audio"
        assert body["current"]["voice_tts"] == "api.google.gemini_flash_tts"

        assert body["voice_lanes"] == {
            "stt_primary": "api.google.gemini_flash_audio",
            "tts_primary": "api.google.gemini_flash_tts",
        }
    finally:
        await client.close()


# ── POST /api/settings/model-ref — chat roles ──────────────────────


async def test_model_ref_swaps_chat_brain_to_anthropic(
    tmp_path: Path, baseline_providers: dict, baseline_roles: dict
) -> None:
    providers, roles = _payload_with_voice_stt(baseline_providers, baseline_roles)
    client = await _make_client(tmp_path, providers, roles)
    try:
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "chat_brain", "ref": "api.anthropic.sonnet_46"},
        )
        assert r.status == 200
        body = await r.json()
        assert body["target"] == "chat_brain"
        assert body["ref"] == "api.anthropic.sonnet_46"
        assert body["model"] == "claude-sonnet-4-6"
        assert body["provider"] == "anthropic"
        assert body["kind"] == "chat"

        doc = _roles_doc(tmp_path)
        assert doc["roles"]["chat_brain"]["primary"] == "api.anthropic.sonnet_46"
        # Old primary pushed to front of fallbacks; original fallbacks preserved.
        fallbacks = doc["roles"]["chat_brain"]["fallbacks"]
        assert fallbacks[0] == "api.openai.gpt54_mini"
        assert "api.openai.gpt54_nano" in fallbacks
        assert "api.google.gemini_25_flash" in fallbacks
    finally:
        await client.close()


async def test_model_ref_dedupes_new_ref_from_fallbacks(
    tmp_path: Path, baseline_providers: dict, baseline_roles: dict
) -> None:
    """If the new primary was already in fallbacks, it should be removed
    from there so the chain doesn't carry a duplicate of itself."""
    providers, roles = _payload_with_voice_stt(baseline_providers, baseline_roles)
    client = await _make_client(tmp_path, providers, roles)
    try:
        # gpt54_nano starts in chat_brain.fallbacks; promoting it should
        # remove it from the fallback list.
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "chat_brain", "ref": "api.openai.gpt54_nano"},
        )
        assert r.status == 200

        doc = _roles_doc(tmp_path)
        assert doc["roles"]["chat_brain"]["primary"] == "api.openai.gpt54_nano"
        fallbacks = doc["roles"]["chat_brain"]["fallbacks"]
        assert "api.openai.gpt54_nano" not in fallbacks
        assert "api.openai.gpt54_mini" in fallbacks  # old primary
        assert "api.google.gemini_25_flash" in fallbacks  # original fallback preserved
    finally:
        await client.close()


async def test_model_ref_idempotent_when_ref_unchanged(
    tmp_path: Path, baseline_providers: dict, baseline_roles: dict
) -> None:
    providers, roles = _payload_with_voice_stt(baseline_providers, baseline_roles)
    client = await _make_client(tmp_path, providers, roles)
    try:
        before_doc = _roles_doc(tmp_path)
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "chat_brain", "ref": "api.openai.gpt54_mini"},
        )
        assert r.status == 200
        # Same ref → mutator returns early; primary + fallbacks must be
        # identical (the file gets re-serialized by ruamel, so byte-equality
        # isn't guaranteed, but the parsed document must match).
        after_doc = _roles_doc(tmp_path)
        assert after_doc["roles"]["chat_brain"] == before_doc["roles"]["chat_brain"]
    finally:
        await client.close()


async def test_model_ref_swaps_observer_and_subagents(
    tmp_path: Path, baseline_providers: dict, baseline_roles: dict
) -> None:
    providers, roles = _payload_with_voice_stt(baseline_providers, baseline_roles)
    client = await _make_client(tmp_path, providers, roles)
    try:
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "observer_agent", "ref": "api.google.gemini_25_flash"},
        )
        assert r.status == 200

        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "subagents_default", "ref": "api.openai.gpt54_mini"},
        )
        assert r.status == 200

        doc = _roles_doc(tmp_path)
        assert doc["roles"]["observer_agent"]["primary"] == "api.google.gemini_25_flash"
        assert doc["roles"]["subagents_default"]["primary"] == "api.openai.gpt54_mini"
    finally:
        await client.close()


# ── POST /api/settings/model-ref — embeddings + voice ─────────────


async def test_model_ref_swaps_embeddings(
    tmp_path: Path, baseline_providers: dict, baseline_roles: dict
) -> None:
    """Add a second embedding entry to the catalog so a real swap is testable."""
    providers, roles = _payload_with_voice_stt(baseline_providers, baseline_roles)
    providers["local"]["ollama"]["models"]["bge_small"] = {
        "kind": "embedding",
        "model": "bge-small-en",
        "dimensions": 384,
    }
    client = await _make_client(tmp_path, providers, roles)
    try:
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "embeddings", "ref": "local.ollama.bge_small"},
        )
        assert r.status == 200
        doc = _roles_doc(tmp_path)
        assert doc["embeddings"]["primary"] == "local.ollama.bge_small"
    finally:
        await client.close()


async def test_model_ref_writes_voice_lanes(
    tmp_path: Path, baseline_providers: dict, baseline_roles: dict
) -> None:
    """voice_stt + voice_tts targets promote `ref` to `voice.<lane>.primary`
    and push the old primary to the front of `fallbacks` (mirrors the
    chat_brain swap path). Per-ref `settings:` survive untouched."""
    providers, roles = _payload_with_voice_stt(baseline_providers, baseline_roles)
    # Add a second TTS catalog entry so we can really swap.
    providers["api"]["elevenlabs"] = {
        "api_key_env": "ELEVENLABS_API_KEY",
        "timeout_seconds": 15,
        "max_retries": 3,
        "adapter": "elevenlabs",
        "models": {
            "flash_v25": {
                "kind": "tts",
                "model": "eleven_flash_v2_5",
                "cost_per_million_chars": 30.0,
            },
        },
    }
    client = await _make_client(tmp_path, providers, roles)
    try:
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "voice_tts", "ref": "api.elevenlabs.flash_v25"},
        )
        assert r.status == 200, await r.text()
        doc = _roles_doc(tmp_path)
        assert doc["voice"]["tts"]["primary"] == "api.elevenlabs.flash_v25"
        assert "api.google.gemini_flash_tts" in (doc["voice"]["tts"].get("fallbacks") or [])

        # voice_stt currently points at gemini_flash_audio; swap is identity
        # so primary stays put (mutator returns early on no-op).
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "voice_stt", "ref": "api.google.gemini_flash_audio"},
        )
        assert r.status == 200
        doc = _roles_doc(tmp_path)
        assert doc["voice"]["stt"]["primary"] == "api.google.gemini_flash_audio"
    finally:
        await client.close()


# ── Validation ───────────────────────────────────────────────────


async def test_model_ref_rejects_kind_mismatch(
    tmp_path: Path, baseline_providers: dict, baseline_roles: dict
) -> None:
    providers, roles = _payload_with_voice_stt(baseline_providers, baseline_roles)
    client = await _make_client(tmp_path, providers, roles)
    try:
        # chat_brain expects kind=chat; embedding ref must be rejected.
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "chat_brain", "ref": "local.ollama.nomic_embed"},
        )
        assert r.status == 400
        body = await r.json()
        assert "kind" in body["error"]

        # voice_tts expects kind=tts; chat ref must be rejected.
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "voice_tts", "ref": "api.openai.gpt54_mini"},
        )
        assert r.status == 400
    finally:
        await client.close()


async def test_model_ref_rejects_invalid_target(
    tmp_path: Path, baseline_providers: dict, baseline_roles: dict
) -> None:
    providers, roles = _payload_with_voice_stt(baseline_providers, baseline_roles)
    client = await _make_client(tmp_path, providers, roles)
    try:
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "soul", "ref": "api.openai.gpt54_mini"},
        )
        assert r.status == 400
    finally:
        await client.close()


async def test_model_ref_rejects_missing_catalog_entry(
    tmp_path: Path, baseline_providers: dict, baseline_roles: dict
) -> None:
    providers, roles = _payload_with_voice_stt(baseline_providers, baseline_roles)
    client = await _make_client(tmp_path, providers, roles)
    try:
        r = await client.post(
            "/api/settings/model-ref",
            json={"target": "chat_brain", "ref": "api.openai.does_not_exist"},
        )
        assert r.status == 400
        body = await r.json()
        assert "missing" in body["error"]
    finally:
        await client.close()
