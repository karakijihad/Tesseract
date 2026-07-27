"""Env-var interpolation in providers.yaml: ${VAR} requires env presence,
${VAR:-default} falls back, non-string values pass through.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tesseract.config.loader import (
    ConfigError,
    load_config,
    resolve_env,
)


def test_resolve_env_with_default_falls_back(monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert resolve_env("${OLLAMA_BASE_URL:-http://localhost:11434}") == "http://localhost:11434"


def test_resolve_env_uses_set_value(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://lan-host:11434")
    assert resolve_env("${OLLAMA_BASE_URL:-http://localhost:11434}") == "http://lan-host:11434"


def test_resolve_env_no_default_unset_raises(monkeypatch) -> None:
    monkeypatch.delenv("MUST_BE_SET", raising=False)
    with pytest.raises(ConfigError, match="MUST_BE_SET"):
        resolve_env("${MUST_BE_SET}")


def test_resolve_env_passes_through_non_strings() -> None:
    assert resolve_env(60) == 60
    assert resolve_env(None) is None
    assert resolve_env(True) is True


def test_resolve_env_passes_through_plain_strings() -> None:
    assert resolve_env("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert resolve_env("") == ""


def test_load_config_resolves_provider_base_urls(
    tmp_path: Path, baseline_providers, baseline_roles, monkeypatch
) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://lan-host:11434")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    pp = tmp_path / "providers.yaml"
    rp = tmp_path / "roles.yaml"
    pp.write_text(yaml.safe_dump(baseline_providers), encoding="utf-8")
    rp.write_text(yaml.safe_dump(baseline_roles), encoding="utf-8")

    bundle = load_config(providers_path=pp, roles_path=rp)
    embed_conn = bundle.embeddings.connection
    assert embed_conn.base_url == "http://lan-host:11434"

    chat_conn = bundle.role("chat_brain").primary.connection
    assert chat_conn.base_url == "https://api.openai.com/v1"  # default branch
