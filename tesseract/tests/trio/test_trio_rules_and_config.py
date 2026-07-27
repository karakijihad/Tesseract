"""W2 — trio verify-loop surface: relay config loader, the `# Trio` prompt
block, and the rules-card ordering contract (output-contract stays last)."""

from __future__ import annotations

import pytest

from tesseract.brain import prompt as prompt_module
from tesseract.config import cockpit as cockpit_module


def test_load_trio_relay_reads_production_config():
    relay = cockpit_module.load_trio_relay()
    assert isinstance(relay["max_rounds"], int) and relay["max_rounds"] >= 1
    assert isinstance(relay["verify_by_default"], bool)


def test_load_trio_relay_raises_on_missing_key(tmp_path, monkeypatch):
    bad = tmp_path / "cockpit.yaml"
    bad.write_text("trio:\n  lanes: []\n", encoding="utf-8")
    monkeypatch.setattr(cockpit_module, "_COCKPIT_YAML", bad)
    with pytest.raises(KeyError):
        cockpit_module.load_trio_relay()


def test_trio_block_renders_lanes_and_relay():
    block = prompt_module._build_trio_block()
    assert block.startswith("# Trio")
    for lane in cockpit_module.load_trio_lanes():
        assert lane["name"] in block
        assert lane["role"] in block
    relay = cockpit_module.load_trio_relay()
    assert f"relay round cap: {relay['max_rounds']}" in block


def test_load_trio_relay_rejects_quoted_bool(tmp_path, monkeypatch):
    # M8: a quoted "false" is a YAML string, not a boolean — bool("false") was
    # True. Strict validation must reject it rather than silently enabling relay.
    bad = tmp_path / "cockpit.yaml"
    bad.write_text(
        'trio:\n  relay:\n    max_rounds: 3\n    verify_by_default: "false"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cockpit_module, "_COCKPIT_YAML", bad)
    with pytest.raises(Exception):
        cockpit_module.load_trio_relay()


def test_load_trio_relay_rejects_nonpositive_cap(tmp_path, monkeypatch):
    # M8: a zero/negative round cap is meaningless — must fail, not be accepted.
    bad = tmp_path / "cockpit.yaml"
    bad.write_text(
        "trio:\n  relay:\n    max_rounds: 0\n    verify_by_default: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cockpit_module, "_COCKPIT_YAML", bad)
    with pytest.raises(Exception):
        cockpit_module.load_trio_relay()


def test_trio_block_surfaces_config_error(tmp_path, monkeypatch):
    # M8: config is authoritative — a broken trio config must surface visibly in
    # the prompt (relay disabled), never silently drop the whole block to "".
    monkeypatch.setattr(cockpit_module, "_COCKPIT_YAML", tmp_path / "missing.yaml")
    block = prompt_module._build_trio_block()
    assert block.startswith("# Trio")
    lowered = block.lower()
    assert "unavailable" in lowered or "disabled" in lowered


def test_trio_card_loads_between_delegation_and_output_contract():
    blobs = prompt_module._load_rules(prompt_module.RULES_DIR)
    joined_index = {
        "delegation": next(i for i, b in enumerate(blobs) if "Parallel delegation" in b),
        "trio": next(i for i, b in enumerate(blobs) if "Trio verification" in b),
    }
    assert joined_index["delegation"] < joined_index["trio"]
    # The output contract must still load LAST (operator ordering contract).
    assert "Output contract" in blobs[-1]


def test_trio_lane_model_derived_from_role_not_hardcoded():
    # N1 — cockpit.yaml stores model_role (roles are pillars); the loader
    # derives the concrete model from roles.yaml, so no model id is hardcoded
    # (and can't drift from the role wiring).
    import yaml

    raw = yaml.safe_load(cockpit_module._COCKPIT_YAML.read_text(encoding="utf-8"))
    for lane in raw["trio"]["lanes"]:
        assert "model" not in lane, "cockpit.yaml must not hardcode a lane model (N1)"
        assert lane.get("model_role"), "each trio lane must reference a model_role"
    # The loader resolves a concrete model from the role wiring.
    for lane in cockpit_module.load_trio_lanes():
        assert lane.get("model"), f"lane {lane['name']} model not derived from role"


def test_trio_card_is_role_agnostic():
    """Roles are pillars — the card must not hardcode lane names, model
    ids, or numeric caps; those live in cockpit.yaml / the Trio block."""
    card = (prompt_module.RULES_DIR / "05a-trio-verification.md").read_text(
        encoding="utf-8"
    )
    for forbidden in ("coder/claude", "auditor/codex", "claude-", "gpt-", "codex-mini"):
        assert forbidden not in card, f"hardcoded value in rules card: {forbidden}"
