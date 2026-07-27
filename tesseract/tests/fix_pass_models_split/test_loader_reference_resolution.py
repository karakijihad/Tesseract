"""Reference resolution: <tier>.<provider>.<model> must resolve to a typed
ResolvedRef, broken refs must raise ConfigError, role-level overrides layer
correctly on top of provider catalog values.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tesseract.config.loader import (
    ConfigBundle,
    ConfigError,
    ProviderConnection,
    ProviderModel,
    ResolvedRef,
    RoleConfig,
    load_config,
)


def test_happy_path_resolves_full_bundle(config_files: tuple[Path, Path]) -> None:
    pp, rp = config_files
    bundle = load_config(providers_path=pp, roles_path=rp)
    assert isinstance(bundle, ConfigBundle)

    chat = bundle.role("chat_brain")
    assert isinstance(chat, RoleConfig)
    assert chat.primary.ref == "api.openai.gpt54_mini"
    assert chat.primary.connection.tier == "api"
    assert chat.primary.connection.name == "openai"
    assert chat.primary.connection.adapter == "openai"
    assert chat.primary.model.id == "gpt54_mini"
    assert chat.primary.model.model == "gpt-5.4-mini"
    assert chat.primary.model.fields["cost_per_mtok_in"] == 0.75
    assert tuple(f.ref for f in chat.fallbacks) == (
        "api.openai.gpt54_nano",
        "api.google.gemini_25_flash",
    )

    assert bundle.embeddings.ref == "local.ollama.nomic_embed"
    assert bundle.embeddings.model.kind == "embedding"
    assert bundle.embeddings.model.fields["dimensions"] == 768

    assert bundle.availability == {"max_consecutive_failures": 3}
    assert bundle.cost_tracking["warning_at_pct"] == 0.75


def test_overrides_carry_through(config_files: tuple[Path, Path]) -> None:
    bundle = load_config(*config_files)
    chat = bundle.role("chat_brain")
    assert chat.overrides["compact_threshold"] == 0.4
    assert chat.overrides["keep_recent_turns"] == 10
    assert chat.overrides["daily_budget_usd"] == 3.0

    obs = bundle.role("observer_agent")
    assert obs.overrides["reasoning_effort_override"] == "low"
    assert obs.overrides["daily_budget_usd"] == 1.0


def test_voice_lanes_resolve(config_files: tuple[Path, Path]) -> None:
    bundle = load_config(*config_files)
    assert bundle.voice is not None
    assert bundle.voice.default_voice_id == "Charon"
    tts = bundle.voice.tts
    assert tts is not None
    assert tts.mode == "active"
    assert tts.primary.ref.ref == "api.google.gemini_flash_tts"
    assert tts.primary.ref.model.kind == "tts"
    assert tts.primary.daily_budget_usd == 1.00
    assert tts.primary.settings["voice_id"] == "Charon"


def test_resolve_ref_method(config_files: tuple[Path, Path]) -> None:
    bundle = load_config(*config_files)
    ref = bundle.resolve("cli.claude.opus_47")
    assert ref.connection.command == "claude"
    assert ref.connection.stream_json_capable is True
    assert ref.model.model == "claude-opus-4-7"


def test_all_models_flattens_catalog(config_files: tuple[Path, Path]) -> None:
    bundle = load_config(*config_files)
    flat = bundle.all_models()
    refs = sorted(ref for ref, _, _ in flat)
    assert refs == [
        "api.google.gemini_25_flash",
        "api.google.gemini_flash_tts",
        "api.openai.gpt54_mini",
        "api.openai.gpt54_nano",
        "cli.claude.opus_47",
        "local.ollama.nomic_embed",
    ]


@pytest.mark.parametrize(
    "bad_ref",
    [
        "api.openai.does_not_exist",
        "api.unknown.gpt54_mini",
        "wrongtier.openai.gpt54_mini",
        "api.openai",
        "api.openai.gpt54_mini.extra",
        "GPT54-MINI",
        "",
    ],
)
def test_invalid_refs_raise_with_path(
    tmp_path: Path, baseline_providers, baseline_roles, bad_ref: str
) -> None:
    """Both shape errors and missing-target errors raise ConfigError."""
    pp = tmp_path / "providers.yaml"
    rp = tmp_path / "roles.yaml"
    pp.write_text(yaml.safe_dump(baseline_providers), encoding="utf-8")
    bad_roles = baseline_roles.copy()
    bad_roles["roles"] = {**bad_roles["roles"]}
    bad_roles["roles"]["chat_brain"] = {**bad_roles["roles"]["chat_brain"], "primary": bad_ref}
    rp.write_text(yaml.safe_dump(bad_roles), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(providers_path=pp, roles_path=rp)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="providers.yaml"):
        load_config(providers_path=tmp_path / "nope.yaml", roles_path=tmp_path / "also-nope.yaml")


def test_role_without_primary_raises(
    tmp_path: Path, baseline_providers, baseline_roles
) -> None:
    pp = tmp_path / "providers.yaml"
    rp = tmp_path / "roles.yaml"
    pp.write_text(yaml.safe_dump(baseline_providers), encoding="utf-8")
    bad = baseline_roles.copy()
    bad["roles"] = {**bad["roles"]}
    bad["roles"]["chat_brain"] = {k: v for k, v in bad["roles"]["chat_brain"].items() if k != "primary"}
    rp.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(ConfigError, match="primary"):
        load_config(providers_path=pp, roles_path=rp)


def test_provider_without_adapter_raises(
    tmp_path: Path, baseline_providers, baseline_roles
) -> None:
    pp = tmp_path / "providers.yaml"
    rp = tmp_path / "roles.yaml"
    bad_prov = baseline_providers.copy()
    bad_prov["api"] = {**bad_prov["api"]}
    bad_prov["api"]["openai"] = {
        k: v for k, v in bad_prov["api"]["openai"].items() if k != "adapter"
    }
    pp.write_text(yaml.safe_dump(bad_prov), encoding="utf-8")
    rp.write_text(yaml.safe_dump(baseline_roles), encoding="utf-8")
    with pytest.raises(ConfigError, match="adapter"):
        load_config(providers_path=pp, roles_path=rp)
