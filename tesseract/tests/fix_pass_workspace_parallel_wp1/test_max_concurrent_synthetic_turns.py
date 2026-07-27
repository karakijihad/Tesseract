"""WP-2-prep-2 — runtime.yaml::max_concurrent_synthetic_turns loader.

Per CLAUDE.md "no hardcoded defaults for infrastructure values; raise loudly
on missing keys" — the loader must raise on missing/invalid values, never
silently fall back.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_loader_returns_int_when_key_present(tmp_path: Path) -> None:
    from tesseract.config.runtime_limits import (
        load_max_concurrent_synthetic_turns,
    )

    p = tmp_path / "runtime.yaml"
    _write_yaml(p, "max_concurrent_synthetic_turns: 3\n")
    assert load_max_concurrent_synthetic_turns(p) == 3


def test_loader_raises_when_key_missing(tmp_path: Path) -> None:
    from tesseract.config.runtime_limits import (
        load_max_concurrent_synthetic_turns,
    )

    p = tmp_path / "runtime.yaml"
    _write_yaml(p, "spawn_stall_seconds: 900\n")
    with pytest.raises(ValueError, match="missing 'max_concurrent_synthetic_turns'"):
        load_max_concurrent_synthetic_turns(p)


def test_loader_raises_when_value_not_int(tmp_path: Path) -> None:
    from tesseract.config.runtime_limits import (
        load_max_concurrent_synthetic_turns,
    )

    p = tmp_path / "runtime.yaml"
    _write_yaml(p, "max_concurrent_synthetic_turns: not-a-number\n")
    with pytest.raises(ValueError, match="must be int"):
        load_max_concurrent_synthetic_turns(p)


def test_loader_raises_when_value_zero_or_negative(tmp_path: Path) -> None:
    from tesseract.config.runtime_limits import (
        load_max_concurrent_synthetic_turns,
    )

    p = tmp_path / "runtime.yaml"
    _write_yaml(p, "max_concurrent_synthetic_turns: 0\n")
    with pytest.raises(ValueError, match=">=1"):
        load_max_concurrent_synthetic_turns(p)


def test_loader_raises_when_file_missing(tmp_path: Path) -> None:
    from tesseract.config.runtime_limits import (
        load_max_concurrent_synthetic_turns,
    )

    p = tmp_path / "absent.yaml"
    with pytest.raises(FileNotFoundError):
        load_max_concurrent_synthetic_turns(p)


def test_canonical_runtime_yaml_has_the_key() -> None:
    """The shipped runtime.yaml must define the key — boot would crash otherwise."""
    from tesseract.config.runtime_limits import (
        default_runtime_config_path,
        load_max_concurrent_synthetic_turns,
    )

    value = load_max_concurrent_synthetic_turns(default_runtime_config_path())
    assert value >= 1
