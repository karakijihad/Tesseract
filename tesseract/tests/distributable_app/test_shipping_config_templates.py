"""Task 8b/8c — shipping config templates: mechanism tests.

Templates under `tesseract/config/_shipping/` are the sole source for the
output tree's `tesseract/config/*.yaml` — every live config file has one, no
exceptions. A missing template is a hard build failure — see
`make_shipping_config.build_shipping_config`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from tesseract.paths import ROOT
from tesseract.scripts.build_production_tree import build
from tesseract.scripts.make_shipping_config import build_shipping_config

_LIVE_CONFIG_DIR = ROOT / "tesseract" / "config"
_SHIPPING_DIR = _LIVE_CONFIG_DIR / "_shipping"


def _live_yaml_names() -> list[str]:
    return sorted(p.name for p in _LIVE_CONFIG_DIR.glob("*.yaml"))


def test_every_shipped_config_has_a_template() -> None:
    """Locks the Task 8c outcome: no allowance list, no exceptions — every
    live `tesseract/config/*.yaml` must have a matching `_shipping/` template."""
    missing = [name for name in _live_yaml_names() if not (_SHIPPING_DIR / name).is_file()]
    assert not missing, f"missing _shipping/ templates for: {missing}"


def test_builder_fails_loudly_on_missing_template(tmp_path: Path) -> None:
    """Delete one template from a full copy of the real config tree and
    confirm the builder raises rather than silently shipping the live file
    or skipping it."""
    src_dir = tmp_path / "config"
    shutil.copytree(_LIVE_CONFIG_DIR, src_dir, ignore=shutil.ignore_patterns("__pycache__"))
    (src_dir / "_shipping" / "mirror.yaml").unlink()

    with pytest.raises(RuntimeError):
        build_shipping_config(src_dir, tmp_path / "out")


def _flatten_keys(node: object, prefix: str = "") -> set[str]:
    """Dotted key paths for a parsed YAML dict. Lists are recorded as a
    single leaf (their own presence/absence is what's compared, not
    per-item structure) — a template may deliberately ship an empty list
    where the live file has entries (e.g. autonomy-watchlist.yaml::sources).
    """
    if isinstance(node, dict):
        keys: set[str] = set()
        for k, v in node.items():
            child_prefix = f"{prefix}.{k}" if prefix else str(k)
            keys |= _flatten_keys(v, child_prefix)
        return keys
    return {prefix}


def test_shipped_config_key_parity() -> None:
    yaml = YAML()
    for name in _live_yaml_names():
        template = _SHIPPING_DIR / name
        assert template.is_file(), f"missing template: {name}"
        live = yaml.load((_LIVE_CONFIG_DIR / name).read_text(encoding="utf-8"))
        shipped = yaml.load(template.read_text(encoding="utf-8"))
        live_keys = _flatten_keys(live)
        shipped_keys = _flatten_keys(shipped)
        assert shipped_keys == live_keys, (
            f"{name}: key structure drifted between the live config and its "
            f"_shipping template — live only: {live_keys - shipped_keys}; "
            f"template only: {shipped_keys - live_keys}"
        )


def test_no_live_config_values_ship(tmp_path: Path) -> None:
    out = tmp_path / "prod"
    build(ROOT, out)
    out_config = out / "tesseract" / "config"
    for name in _live_yaml_names():
        shipped_bytes = (out_config / name).read_bytes()
        template_bytes = (_SHIPPING_DIR / name).read_bytes()
        assert shipped_bytes == template_bytes, f"{name}: output config is not byte-identical to its _shipping template"
        live_bytes = (_LIVE_CONFIG_DIR / name).read_bytes()
        if live_bytes != template_bytes:
            assert shipped_bytes != live_bytes, f"{name}: shipped bytes match the LIVE config, not the template"


def test_shipping_dir_itself_does_not_ship_as_a_subfolder(tmp_path: Path) -> None:
    out = tmp_path / "prod"
    build(ROOT, out)
    assert not (out / "tesseract" / "config" / "_shipping").exists()


def test_permissions_yaml_values_are_byte_for_byte_identical() -> None:
    """permissions.yaml is the security authority — Task 8c's template may
    only reword comments, with one deliberate, disclosed exception:
    top-level `security_mode`. The operator's live config runs `headless`
    (full autonomy, minimal prompts) for their own attended machine; the
    shipped template pins `max` instead, because a friend's first install
    should see and approve writes/outbound/subprocess calls before trusting
    the system with headless auto-allow. Every OTHER value — every tool's
    AUTO/ASK/DENY posture, every `path_overrides` entry, both
    bash_readonly_allowlist lists, pty_thresholds, loop_cost_caps — must
    still be byte-for-byte value-identical, so a future comment-only edit
    can never drift anything else into the shipped product.
    """
    yaml = YAML(typ="safe")
    live = yaml.load((_LIVE_CONFIG_DIR / "permissions.yaml").read_text(encoding="utf-8"))
    shipped = yaml.load((_SHIPPING_DIR / "permissions.yaml").read_text(encoding="utf-8"))

    live_rest = {k: v for k, v in live.items() if k != "security_mode"}
    shipped_rest = {k: v for k, v in shipped.items() if k != "security_mode"}
    assert shipped_rest == live_rest, "permissions.yaml: shipped VALUES differ from the live config outside security_mode"

    # The real guard: shipping full autonomy by default is never acceptable,
    # regardless of what the operator's own live config runs.
    assert shipped["security_mode"] in {"max", "standard"}, "permissions.yaml: shipped security_mode must not be headless"
    assert shipped["security_mode"] != "headless"
