"""Stage 10 — `agent_pending_cap` loader contract.

Headless agent proposals are capped by `runtime.yaml::agent_pending_cap`
so an unattended loop cannot flood `agents/pending/`. Raise-loudly
semantics per CLAUDE.md: no hardcoded infrastructure defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_agent_pending_cap,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_value_round_trips(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "runtime.yaml", "agent_pending_cap: 7\n")
    assert load_agent_pending_cap(cfg) == 7


def test_missing_key_raises(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "runtime.yaml", "max_spawn_depth: 3\n")
    with pytest.raises(ValueError, match="agent_pending_cap"):
        load_agent_pending_cap(cfg)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_agent_pending_cap(tmp_path / "nope.yaml")


@pytest.mark.parametrize("bad", ["0", "-2", "'x'"])
def test_invalid_values_rejected(tmp_path: Path, bad: str) -> None:
    cfg = _write(tmp_path / "runtime.yaml", f"agent_pending_cap: {bad}\n")
    with pytest.raises(ValueError):
        load_agent_pending_cap(cfg)


def test_shipped_runtime_yaml_has_key() -> None:
    """The real config must carry the key — boot would raise otherwise."""
    assert load_agent_pending_cap(default_runtime_config_path()) >= 1
