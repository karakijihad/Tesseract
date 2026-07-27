"""CV-1 — cockpit.yaml is the single source of truth for the trio models.

Locks the de-duplication the reviewer flagged: the mission workers and the
Mirror lane bridge both read trio models from cockpit.yaml, so a model
upgrade is a one-line YAML edit (no drifting source literals)."""

from __future__ import annotations

import pytest

from tesseract.config.cockpit import load_trio_lanes, trio_lane


def test_trio_defines_the_two_named_lanes():
    names = {l["name"] for l in load_trio_lanes()}
    assert names == {"coder/claude", "auditor/codex"}


def test_each_lane_has_kind_and_model():
    for lane in load_trio_lanes():
        assert lane["kind"] in ("claude", "codex")
        assert isinstance(lane["model"], str) and lane["model"]


def test_unknown_lane_raises():
    with pytest.raises(KeyError):
        trio_lane("nope/none")
