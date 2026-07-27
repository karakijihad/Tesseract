"""Task 2B Part E — vet_score / vet_weight scoring component."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tesseract.orchestrator.autonomy.scoring import AgendaWeights, score_item
from tesseract.tests.fix_pass_autonomy_AU_4.conftest import make_item


def test_vet_component_scales_with_vet_score() -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    weights = AgendaWeights(vet_weight=12.0)

    high = make_item(now=now)
    high.vet_score = 0.9
    low = make_item(now=now)
    low.vet_score = 0.0

    _, high_components = score_item(high, weights, now=now)
    _, low_components = score_item(low, weights, now=now)

    assert pytest.approx(high_components["vet"] - low_components["vet"]) == 10.8


def test_default_vet_weight_is_zero_noop() -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    item = make_item(now=now)
    item.vet_score = 0.9
    _, components = score_item(item, AgendaWeights(), now=now)
    assert components["vet"] == 0.0
