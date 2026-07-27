"""Voice-cost settings endpoint regression tests.

`POST /api/settings/voice-cost` writes per-provider voice pricing + daily
caps. The path supports partial updates (operator changes only the cap,
leaves the rate alone) and rejects (a) unknown providers, (b) per-provider
caps exceeding the global cap, (c) negative rates, (d) entirely empty
field sets.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import settings as settings_route


def _seed_split_yaml(config_dir: Path) -> tuple[Path, Path]:
    providers = {
        "availability": {"max_consecutive_failures": 3},
        "cost_tracking": {
            "enabled": True,
            "warning_at_pct": 0.75,
            "log_file": "logs/cost.jsonl",
        },
        "api": {
            "google": {
                "api_key_env": "GOOGLE_API_KEY",
                "timeout_seconds": 60,
                "max_retries": 3,
                "adapter": "gemini",
                "models": {
                    "gemini_flash_tts": {
                        "kind": "tts",
                        "model": "gemini-2.5-flash-preview-tts",
                        "cost_per_million_chars": 10.0,
                    },
                    "gemini_flash_audio": {
                        "kind": "audio_stt",
                        "model": "gemini-2.5-flash",
                        "cost_per_audio_hour": 0.09,
                    },
                },
            },
        },
        "local": {
            "ollama": {
                "base_url": "http://localhost:11434",
                "timeout_seconds": 60,
                "max_retries": 3,
                "host": "this_pc",
                "adapter": "ollama",
                "models": {
                    "nomic_embed": {
                        "kind": "embedding",
                        "model": "nomic-embed-text",
                        "dimensions": 768,
                        "timeout_seconds": 30,
                        "max_retries": 3,
                    },
                },
            },
        },
    }
    roles = {
        "embeddings": {"primary": "local.ollama.nomic_embed"},
        "roles": {
            "chat_brain": {
                "mode": "active",
                "primary": "api.google.gemini_flash_audio",
                "daily_budget_usd": 3.0,
            },
        },
        "voice": {
            "default_voice_id": "Charon",
            "default_tone_prompt": "calm",
            "stt": {
                "mode": "active",
                "primary": "api.google.gemini_flash_audio",
                "settings": {
                    "api.google.gemini_flash_audio": {
                        "daily_budget_usd": 0.3,
                    },
                },
            },
            "tts": {
                "mode": "active",
                "primary": "api.google.gemini_flash_tts",
                "settings": {
                    "api.google.gemini_flash_tts": {
                        "voice_id": "Charon",
                        "daily_budget_usd": 0.2,
                    },
                },
            },
        },
    }
    providers_path = config_dir / "providers.yaml"
    roles_path = config_dir / "roles.yaml"
    providers_path.write_text(yaml.safe_dump(providers), encoding="utf-8")
    roles_path.write_text(yaml.safe_dump(roles), encoding="utf-8")
    return providers_path, roles_path


async def _make_client(tmp_path: Path) -> TestClient:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    providers_path, roles_path = _seed_split_yaml(config_dir)

    from tesseract.config.loader import load_config
    from tesseract.mirror.server.config import synthesize_legacy_models_dict

    bundle = load_config(providers_path=providers_path, roles_path=roles_path)
    cfg = synthesize_legacy_models_dict(bundle)

    app = web.Application()
    app["tesseract_dir"] = tmp_path
    app["config"] = SimpleNamespace(models=cfg)
    app["cost_ledger"] = None
    app.router.add_post("/api/settings/voice-cost", settings_route.set_voice_cost)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


async def test_partial_update_preserves_other_fields(tmp_path: Path) -> None:
    """Operator edits only the cap, not the rate. The rate value must
    survive untouched in models.yaml — clobbering it would silently zero
    or wrong-bill until the next deploy."""
    client = await _make_client(tmp_path)
    try:
        res = await client.post(
            "/api/settings/voice-cost",
            json={"tts": {"gemini_flash_tts": {"daily_budget_usd": 0.4}}},
        )
        assert res.status == 200
        body = await res.json()
        # Response is the full IdentityCostTracking shape so the frontend
        # can refresh `useIdentityStore.costTracking` directly. Voice block
        # uses `rate` / `cap_usd` keys (matching CostLedger.snapshot()),
        # not the yaml-internal `cost_per_million_chars` / `daily_budget_usd`.
        assert body["voice"]["tts"]["gemini_flash_tts"]["rate"] == 10.0
        assert body["voice"]["tts"]["gemini_flash_tts"]["cap_usd"] == 0.4

        # providers.yaml: catalog model rate untouched.
        providers_after = yaml.safe_load(
            (tmp_path / "config" / "providers.yaml").read_text(encoding="utf-8")
        )
        tts_model = providers_after["api"]["google"]["models"]["gemini_flash_tts"]
        assert tts_model["cost_per_million_chars"] == 10.0
        # roles.yaml: lane cap updated.
        roles_after = yaml.safe_load(
            (tmp_path / "config" / "roles.yaml").read_text(encoding="utf-8")
        )
        tts_settings = roles_after["voice"]["tts"]["settings"]
        assert tts_settings["api.google.gemini_flash_tts"]["daily_budget_usd"] == 0.4
    finally:
        await client.close()


async def test_unknown_provider_rejected(tmp_path: Path) -> None:
    """Provider keys must already exist in yaml. Pricing for an unknown
    provider would be a config error — fail loudly."""
    client = await _make_client(tmp_path)
    try:
        res = await client.post(
            "/api/settings/voice-cost",
            json={"tts": {"unknown": {"daily_budget_usd": 0.1}}},
        )
        assert res.status == 400
        body = await res.json()
        assert "unknown TTS provider" in body["error"]
    finally:
        await client.close()


async def test_large_voice_cap_accepted_and_umbrella_grows(tmp_path: Path) -> None:
    """Under the new derived-global model the voice POST no longer rejects
    per-provider caps that exceed any static global value — bumping a voice
    cap simply raises the umbrella by the same amount. A large cap must be
    accepted with 200 and the response must reflect the new cap_usd."""
    client = await _make_client(tmp_path)
    try:
        res = await client.post(
            "/api/settings/voice-cost",
            json={"tts": {"gemini_flash_tts": {"daily_budget_usd": 99.0}}},
        )
        assert res.status == 200
        body = await res.json()
        # The response is the full IdentityCostTracking shape.
        assert body["voice"]["tts"]["gemini_flash_tts"]["cap_usd"] == pytest.approx(99.0)
    finally:
        await client.close()


async def test_negative_rate_rejected(tmp_path: Path) -> None:
    client = await _make_client(tmp_path)
    try:
        res = await client.post(
            "/api/settings/voice-cost",
            json={"stt": {"gemini_flash_audio": {"cost_per_audio_hour": -1.0}}},
        )
        assert res.status == 400
    finally:
        await client.close()


async def test_empty_field_set_rejected_with_helpful_error(tmp_path: Path) -> None:
    """Operator sends `{tts: {provider: {}}}` — provider key is valid
    but no field is set. The 400 message must list the recognized field
    names so the operator doesn't have to read the route source."""
    client = await _make_client(tmp_path)
    try:
        res = await client.post(
            "/api/settings/voice-cost",
            json={"tts": {"gemini_flash_tts": {}}},
        )
        assert res.status == 400
        body = await res.json()
        assert "cost_per_million_chars" in body["error"]
        assert "daily_budget_usd" in body["error"]
    finally:
        await client.close()
