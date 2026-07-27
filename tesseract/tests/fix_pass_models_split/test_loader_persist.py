"""persist() round-trips edits while preserving comments and key order."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as pyyaml

from tesseract.config.loader import (
    ConfigError,
    load_config,
    persist,
    persist_many,
)


def test_persist_writes_leaf(config_files: tuple[Path, Path]) -> None:
    pp, rp = config_files
    persist("roles", "roles.chat_brain.compact_threshold", 0.55,
            providers_path=pp, roles_path=rp)
    bundle = load_config(providers_path=pp, roles_path=rp)
    assert bundle.role("chat_brain").overrides["compact_threshold"] == 0.55


def test_persist_preserves_comments(tmp_path: Path) -> None:
    rp = tmp_path / "roles.yaml"
    pp = tmp_path / "providers.yaml"
    rp.write_text(
        "# top-level comment\n"
        "embeddings:\n"
        "  primary: local.ollama.nomic_embed\n"
        "roles:\n"
        "  chat_brain:\n"
        "    mode: active\n"
        "    primary: api.openai.gpt54_mini  # inline comment\n"
        "    compact_threshold: 0.4\n",
        encoding="utf-8",
    )
    pp.write_text(
        "api:\n  openai:\n    adapter: openai\n    timeout_seconds: 60\n    max_retries: 3\n"
        "    models:\n      gpt54_mini:\n        model: gpt-5.4-mini\n",
        encoding="utf-8",
    )
    persist("roles", "roles.chat_brain.compact_threshold", 0.5,
            providers_path=pp, roles_path=rp)
    text = rp.read_text(encoding="utf-8")
    assert "# top-level comment" in text
    assert "# inline comment" in text
    assert "compact_threshold: 0.5" in text


def test_persist_many_one_round_trip(config_files: tuple[Path, Path]) -> None:
    pp, rp = config_files
    persist_many(
        "roles",
        [
            ("roles.chat_brain.primary", "api.openai.gpt54_nano"),
            ("roles.chat_brain.fallbacks", ["api.openai.gpt54_mini", "api.google.gemini_25_flash"]),
        ],
        providers_path=pp, roles_path=rp,
    )
    bundle = load_config(providers_path=pp, roles_path=rp)
    chat = bundle.role("chat_brain")
    assert chat.primary.ref == "api.openai.gpt54_nano"
    assert tuple(f.ref for f in chat.fallbacks) == (
        "api.openai.gpt54_mini",
        "api.google.gemini_25_flash",
    )


def test_persist_unknown_path_raises(config_files: tuple[Path, Path]) -> None:
    pp, rp = config_files
    with pytest.raises(ConfigError, match="no node at 'nonexistent'"):
        persist("roles", "roles.nonexistent.thing", 1,
                providers_path=pp, roles_path=rp)


def test_persist_writes_to_providers_too(config_files: tuple[Path, Path]) -> None:
    pp, rp = config_files
    persist("providers", "cost_tracking.warning_at_pct", 0.85,
            providers_path=pp, roles_path=rp)
    raw = pyyaml.safe_load(pp.read_text(encoding="utf-8"))
    assert raw["cost_tracking"]["warning_at_pct"] == 0.85
