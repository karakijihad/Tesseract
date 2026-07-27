"""`load_chat_brain_config()` must raise on any missing infrastructure key
in the new providers.yaml + roles.yaml shape.

CLAUDE.md prohibits silent ``.get(key, default)`` fallbacks for infrastructure
values. This test parametrizes over every required catalog/role key and
confirms each produces a ConfigError that names the missing path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as pyyaml

from tesseract.brain import boot as brain_boot
from tesseract.config import loader as config_loader
from tesseract.config.loader import ConfigError


def _write_pair(tmp_path: Path, *, providers: dict, roles: dict) -> tuple[Path, Path]:
    pp = tmp_path / "providers.yaml"
    rp = tmp_path / "roles.yaml"
    pp.write_text(pyyaml.safe_dump(providers), encoding="utf-8")
    rp.write_text(pyyaml.safe_dump(roles), encoding="utf-8")
    return pp, rp


def _good_providers() -> dict:
    return {
        "availability": {"max_consecutive_failures": 3},
        "cost_tracking": {"enabled": True, "warning_at_pct": 0.75, "log_file": "logs/cost-tracking.jsonl"},
        "api": {
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
                "timeout_seconds": 60,
                "max_retries": 3,
                "adapter": "openai",
                "models": {
                    "gpt54_nano": {
                        "model": "gpt-5.4-nano",
                        "context_window": 400000,
                        "max_output_tokens": 8192,
                        "reasoning_effort": "medium",
                        "temperature": 1.0,
                        "knowledge_cutoff": "2025-08-31",
                        "use_responses_api": True,
                        "cost_per_mtok_in": 0.20,
                        "cost_per_mtok_out": 1.25,
                    },
                },
            },
        },
        "local": {
            "ollama": {
                "base_url": "http://localhost:11434",
                "timeout_seconds": 120,
                "max_retries": 3,
                "adapter": "ollama",
                "models": {
                    "nomic_embed": {"kind": "embedding", "model": "nomic-embed-text", "dimensions": 768},
                },
            },
        },
    }


def _good_roles() -> dict:
    return {
        "embeddings": {"primary": "local.ollama.nomic_embed"},
        "roles": {
            "chat_brain": {
                "mode": "active",
                "primary": "api.openai.gpt54_nano",
                "compact_threshold": 0.40,
                "keep_recent_turns": 10,
                "tool_iteration_cap": 25,
                "consecutive_error_cap": 3,
            },
        },
    }


def _point_loader_at(monkeypatch: pytest.MonkeyPatch, pp: Path, rp: Path) -> None:
    monkeypatch.setattr(config_loader, "PROVIDERS_YAML", pp)
    monkeypatch.setattr(config_loader, "ROLES_YAML", rp)
    monkeypatch.setattr(brain_boot, "PROVIDERS_YAML", pp)
    monkeypatch.setattr(brain_boot, "ROLES_YAML", rp)


def test_happy_path_returns_typed_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pp, rp = _write_pair(tmp_path, providers=_good_providers(), roles=_good_roles())
    _point_loader_at(monkeypatch, pp, rp)
    cfg = brain_boot.load_chat_brain_config()
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-5.4-nano"
    assert cfg.context_window == 400000
    assert cfg.temperature == 1.0
    assert cfg.use_responses_api is True
    assert cfg.compact_threshold == 0.40
    assert cfg.keep_recent_turns == 10
    assert cfg.provider_cfg["timeout_seconds"] == 60
    assert cfg.ref.ref == "api.openai.gpt54_nano"


@pytest.mark.parametrize(
    "model_field",
    ["temperature", "max_output_tokens", "context_window"],
)
def test_missing_catalog_field_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model_field: str,
) -> None:
    """Catalog model entries must declare core decoding params."""
    providers = _good_providers()
    del providers["api"]["openai"]["models"]["gpt54_nano"][model_field]
    pp, rp = _write_pair(tmp_path, providers=providers, roles=_good_roles())
    _point_loader_at(monkeypatch, pp, rp)
    with pytest.raises(RuntimeError) as exc:
        brain_boot.load_chat_brain_config()
    assert model_field in str(exc.value)


@pytest.mark.parametrize(
    "conn_field",
    ["adapter", "timeout_seconds", "max_retries"],
)
def test_missing_provider_connection_field_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, conn_field: str,
) -> None:
    """Provider connection blocks must carry adapter + timeout + retries."""
    providers = _good_providers()
    del providers["api"]["openai"][conn_field]
    pp, rp = _write_pair(tmp_path, providers=providers, roles=_good_roles())
    _point_loader_at(monkeypatch, pp, rp)
    with pytest.raises(ConfigError) as exc:
        brain_boot.load_chat_brain_config()
    assert conn_field in str(exc.value)


def test_missing_primary_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    roles = _good_roles()
    del roles["roles"]["chat_brain"]["primary"]
    pp, rp = _write_pair(tmp_path, providers=_good_providers(), roles=roles)
    _point_loader_at(monkeypatch, pp, rp)
    with pytest.raises(ConfigError, match="primary"):
        brain_boot.load_chat_brain_config()


def test_dangling_provider_ref_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reference to a model the catalog doesn't carry must surface at boot."""
    roles = _good_roles()
    roles["roles"]["chat_brain"]["primary"] = "api.openai.does_not_exist"
    pp, rp = _write_pair(tmp_path, providers=_good_providers(), roles=roles)
    _point_loader_at(monkeypatch, pp, rp)
    with pytest.raises(ConfigError, match="does_not_exist"):
        brain_boot.load_chat_brain_config()


def test_adapter_options_from_chat_brain_has_no_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """adapter_options_from_chat_brain must echo every field from the typed cfg."""
    pp, rp = _write_pair(tmp_path, providers=_good_providers(), roles=_good_roles())
    _point_loader_at(monkeypatch, pp, rp)
    cfg = brain_boot.load_chat_brain_config()
    opts = brain_boot.adapter_options_from_chat_brain(cfg)
    assert opts.model == "gpt-5.4-nano"
    assert opts.context_window == 400000
    assert opts.tier == "api"
    assert opts.use_responses_api is True
    assert opts.role == "chat_brain"
