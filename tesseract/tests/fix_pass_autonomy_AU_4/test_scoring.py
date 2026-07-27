"""AU-4 — deterministic scoring formula + monotonicity invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tesseract.orchestrator.autonomy.models import (
    AgendaSource,
    AgendaStatus,
    RiskClass,
)
from tesseract.orchestrator.autonomy.scoring import (
    AgendaWeights,
    score_item,
)
from tesseract.tests.fix_pass_autonomy_AU_4.conftest import make_item


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def weights() -> AgendaWeights:
    return AgendaWeights()


def test_score_is_deterministic(now: datetime, weights: AgendaWeights) -> None:
    item = make_item(now=now)
    s1, c1 = score_item(item, weights, now=now)
    s2, c2 = score_item(item, weights, now=now)
    assert s1 == s2
    assert c1 == c2


def test_components_sum_to_total(now: datetime, weights: AgendaWeights) -> None:
    item = make_item(now=now, operator_priority=3)
    total, components = score_item(item, weights, now=now)
    assert pytest.approx(total) == sum(components.values())


def test_operator_priority_dominates(now: datetime, weights: AgendaWeights) -> None:
    """Operator's +5 nudge MUST outrank a 7-day-old observer item with
    perfect budget headroom."""
    operator_urgent = make_item(
        now=now, operator_priority=5, source=AgendaSource.OPERATOR
    )
    old_observer = make_item(
        now=now - timedelta(hours=168),
        operator_priority=0,
        source=AgendaSource.SELF_REFLECTION,
    )
    s_op, _ = score_item(operator_urgent, weights, now=now)
    s_old, _ = score_item(old_observer, weights, now=now)
    assert s_op > s_old


def test_age_capped_at_seven_days(now: datetime, weights: AgendaWeights) -> None:
    seven_days = make_item(now=now - timedelta(hours=168))
    forever = make_item(now=now - timedelta(hours=10_000))
    s_seven, _ = score_item(seven_days, weights, now=now)
    s_forever, _ = score_item(forever, weights, now=now)
    assert s_seven == s_forever


def test_higher_risk_lowers_score(now: datetime, weights: AgendaWeights) -> None:
    auto = make_item(risk_class=RiskClass.AUTONOMOUS, now=now)
    propose = make_item(risk_class=RiskClass.PROPOSE, now=now)
    gated = make_item(risk_class=RiskClass.OPERATOR_GATE, now=now)
    s_auto, _ = score_item(auto, weights, now=now)
    s_prop, _ = score_item(propose, weights, now=now)
    s_gated, _ = score_item(gated, weights, now=now)
    assert s_auto > s_prop > s_gated


def test_budget_headroom_boosts_score(now: datetime, weights: AgendaWeights) -> None:
    fresh = make_item(now=now, budget_tokens_cap=10000)
    spent = make_item(now=now, budget_tokens_cap=10000)
    spent.budget_tokens_spent = 10000  # fully consumed
    s_fresh, _ = score_item(fresh, weights, now=now)
    s_spent, _ = score_item(spent, weights, now=now)
    assert s_fresh > s_spent


def test_source_trust_recovery_outranks_self_reflection(
    now: datetime, weights: AgendaWeights
) -> None:
    recovery_item = make_item(source=AgendaSource.RECOVERY, now=now)
    self_reflection = make_item(source=AgendaSource.SELF_REFLECTION, now=now)
    s_rec, _ = score_item(recovery_item, weights, now=now)
    s_self, _ = score_item(self_reflection, weights, now=now)
    assert s_rec > s_self


def test_age_score_in_components(now: datetime, weights: AgendaWeights) -> None:
    item = make_item(now=now - timedelta(hours=10))
    _, components = score_item(item, weights, now=now)
    # Default age_weight=1.0, capped at 168h, so 10h of age contributes ~10.
    assert pytest.approx(components["age"], abs=0.5) == 10.0


def test_custom_weights_change_outcome(now: datetime) -> None:
    """Setting risk_weight=0 must collapse the risk component to zero."""
    item = make_item(risk_class=RiskClass.OPERATOR_GATE, now=now)
    flat = AgendaWeights(risk_weight=0.0)
    _, components = score_item(item, flat, now=now)
    assert components["risk"] == 0.0
