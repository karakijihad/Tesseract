"""Tests for the build-time shipping-config templater.

Every shipped `.yaml` under `tesseract/config/` ships from a hand-authored
`_shipping/<name>.yaml` template, copied byte-for-byte (Task 8b/8c). There is
no fallback path and no allowance list — a missing template is always a hard
build failure. See test_shipping_config_templates.py for the mechanism tests
against the real repo tree.
"""

from pathlib import Path

import pytest

from tesseract.scripts.make_shipping_config import build_shipping_config


def test_build_shipping_config_round_trip(tmp_path: Path):
    src_dir = tmp_path / "src"
    out_dir = tmp_path / "out"
    (src_dir / "_shipping").mkdir(parents=True)

    mirror_live_text = "operator_name: Jane Doe\n"
    mirror_template_text = "operator_name: Operator\n"
    (src_dir / "mirror.yaml").write_text(mirror_live_text, encoding="utf-8")
    (src_dir / "_shipping" / "mirror.yaml").write_text(mirror_template_text, encoding="utf-8")

    build_shipping_config(src_dir, out_dir)

    out_mirror = (out_dir / "mirror.yaml").read_text(encoding="utf-8")
    assert out_mirror == mirror_template_text
    assert "Jane Doe" not in out_mirror

    # src_dir must be byte-for-byte untouched.
    assert (src_dir / "mirror.yaml").read_text(encoding="utf-8") == mirror_live_text


def test_build_shipping_config_missing_template_raises(tmp_path: Path):
    src_dir = tmp_path / "src"
    out_dir = tmp_path / "out"
    (src_dir / "_shipping").mkdir(parents=True)
    (src_dir / "providers.yaml").write_text("some: value\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        build_shipping_config(src_dir, out_dir)


def test_build_shipping_config_rejects_same_dir(tmp_path: Path):
    same_dir = tmp_path / "config"
    same_dir.mkdir()
    providers_text = "some: value\n"
    (same_dir / "providers.yaml").write_text(providers_text, encoding="utf-8")

    with pytest.raises(ValueError):
        build_shipping_config(same_dir, same_dir)

    # Must be untouched — the guard fires before any write.
    assert (same_dir / "providers.yaml").read_text(encoding="utf-8") == providers_text
