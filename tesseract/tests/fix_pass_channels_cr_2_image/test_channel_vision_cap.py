"""CR-2: ``channels.yaml::cost.per_role.channel_vision`` is paired with a
matching ``channel_vision`` role entry in ``roles.yaml`` so the cap
actually reaches :class:`CostLedger.per_role_caps`.

Without the roles.yaml entry the cap stated in channels.yaml would be
dead config — the ledger only iterates ``bundle.roles`` to populate
``per_role_caps`` (see ``ledger.py::from_bundle``). The CR-2 reviewer
flagged this as IMPORTANT.
"""

from __future__ import annotations

from tesseract.brain.cost.ledger import CostLedger
from tesseract.config.loader import load_config


def test_channel_vision_role_is_present_in_roles_yaml() -> None:
    bundle = load_config()
    assert "channel_vision" in bundle.roles
    role = bundle.roles["channel_vision"]
    assert role.mode == "inactive"
    assert role.overrides.get("daily_budget_usd") == 0.50


def test_channel_vision_cap_lands_in_cost_ledger_per_role_caps(tmp_path) -> None:
    bundle = load_config()
    ledger = CostLedger.from_bundle(bundle, log_path=tmp_path / "cost.jsonl")
    assert ledger.per_role_caps.get("channel_vision") == 0.50

    state = ledger.budget_state("channel_vision")
    assert state.role_cap_usd == 0.50


def test_channel_vision_cap_in_channels_yaml_matches_roles_yaml() -> None:
    """If an operator edits one side, the other should follow — this test
    catches the drift so the cap doesn't silently desync."""
    from tesseract.integrations._channels_config import load_channels_config

    bundle = load_config()
    role_cap = bundle.roles["channel_vision"].overrides.get("daily_budget_usd")
    channels_cap = load_channels_config().telegram.cost.per_role.get("channel_vision")
    assert role_cap == channels_cap, (
        f"channel_vision cap drift: roles.yaml={role_cap} channels.yaml={channels_cap}"
    )
