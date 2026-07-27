"""Fix 2 — runtime.yaml loader for max_foreground_delegate_timeout_s follows
the raise-loudly contract (fix-pass 2026-07-10)."""

from __future__ import annotations

import pytest

from tesseract.config.runtime_limits import load_max_foreground_delegate_timeout_s


def _write(tmp_path, body: str):
    p = tmp_path / "runtime.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_valid_value(tmp_path):
    p = _write(tmp_path, "max_foreground_delegate_timeout_s: 180\n")
    assert load_max_foreground_delegate_timeout_s(p) == 180.0


def test_missing_key_raises(tmp_path):
    p = _write(tmp_path, "other_key: 1\n")
    with pytest.raises(ValueError, match="max_foreground_delegate_timeout_s"):
        load_max_foreground_delegate_timeout_s(p)


def test_non_numeric_raises(tmp_path):
    p = _write(tmp_path, "max_foreground_delegate_timeout_s: soon\n")
    with pytest.raises(ValueError, match="must be a number"):
        load_max_foreground_delegate_timeout_s(p)


def test_non_positive_raises(tmp_path):
    p = _write(tmp_path, "max_foreground_delegate_timeout_s: 0\n")
    with pytest.raises(ValueError, match="must be > 0"):
        load_max_foreground_delegate_timeout_s(p)


def test_repo_config_has_the_key():
    from tesseract.config.runtime_limits import default_runtime_config_path

    assert load_max_foreground_delegate_timeout_s(default_runtime_config_path()) > 0
