"""mirror-multi-chat P2 inc.C2 — runtime.yaml::max_concurrent_chat_turns_per_provider loader.

Per CLAUDE.md "no hardcoded defaults for infrastructure values; raise loudly on
missing keys" — the per-provider chat-turn concurrency cap must raise on
missing/invalid values, never silently fall back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.config.runtime_limits import (
    load_max_concurrent_chat_turns_per_provider,
)


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_loader_returns_int_when_key_present(tmp_path: Path) -> None:
    p = tmp_path / "runtime.yaml"
    _write(p, "max_concurrent_chat_turns_per_provider: 3\n")
    assert load_max_concurrent_chat_turns_per_provider(p) == 3


def test_loader_raises_when_key_missing(tmp_path: Path) -> None:
    p = tmp_path / "runtime.yaml"
    _write(p, "spawn_stall_seconds: 900\n")
    with pytest.raises(ValueError, match="missing 'max_concurrent_chat_turns_per_provider'"):
        load_max_concurrent_chat_turns_per_provider(p)


def test_loader_rejects_non_int(tmp_path: Path) -> None:
    p = tmp_path / "runtime.yaml"
    _write(p, "max_concurrent_chat_turns_per_provider: nope\n")
    with pytest.raises(ValueError, match="must be int"):
        load_max_concurrent_chat_turns_per_provider(p)


def test_loader_rejects_below_one(tmp_path: Path) -> None:
    p = tmp_path / "runtime.yaml"
    _write(p, "max_concurrent_chat_turns_per_provider: 0\n")
    with pytest.raises(ValueError, match="must be >=1"):
        load_max_concurrent_chat_turns_per_provider(p)


def test_default_runtime_config_has_the_key() -> None:
    from tesseract.config.runtime_limits import default_runtime_config_path

    assert load_max_concurrent_chat_turns_per_provider(default_runtime_config_path()) >= 1
