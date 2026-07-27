"""Deterministic priority scoring for AgendaItem.

Pure function: ``score_item(item, weights, now) → (score, components)``.
Same inputs MUST produce the same score — the dashboard surfaces the
breakdown so the operator can see why X ranked above Y.

Defaults live in ``tesseract/config/agenda.yaml``; tests construct
``AgendaWeights`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    RiskClass,
)

DEFAULT_AGE_CAP_HOURS: float = 168.0  # 7 days


_RISK_SCORE: dict[RiskClass, float] = {
    RiskClass.AUTONOMOUS: 0.0,
    RiskClass.PROPOSE: 1.0,
    RiskClass.OPERATOR_GATE: 3.0,
    RiskClass.ABSOLUTE_DENY: 1e9,
}


_DEFAULT_SOURCE_TRUST: dict[AgendaSource, float] = {
    AgendaSource.OPERATOR: 1.0,
    AgendaSource.PROVIDER_WATCH: 0.6,
    AgendaSource.VAULT_SIGNAL: 0.3,
    AgendaSource.RECOVERY: 0.95,
    AgendaSource.SELF_REFLECTION: 0.2,
    # operator_view — operator-attention signals from the Mirror WS
    # (long-dwell, paired_with_failure). Trust on par with channel
    # because the operator is directly involved in producing the
    # signal, but it's still ambient — not an explicit ask.
    AgendaSource.OPERATOR_VIEW: 0.5,
    # AU-23 — strategist is a deliberate weekly curator (1-3 high-conviction
    # initiatives). Trust above ambient self_reflection because every
    # initiative ships with operator_review and explicit success criteria;
    # below scheduler/operator because the operator hasn't yet endorsed it.
    AgendaSource.STRATEGIST: 0.65,
    # P7 Task 2 — Codex-grounded research pass (outdated deps, upstream
    # releases, applicable improvements). Above ambient self_reflection
    # (pattern-matching over recent activity) because findings are
    # evidence-grounded in an actual read of the target; below
    # provider_watch because it's still an autonomous ambient sweep, not
    # an explicit operator ask.
    AgendaSource.REPO_UPGRADE: 0.4,
    # P7 Task 2b — identity-anchored web discovery (query-gen + evaluation
    # over web_search/tavily + optional feeds). Less grounded than
    # repo_upgrade (no direct read of a target); above ambient
    # self_reflection because every proposal carries an explicit
    # "why us / why now" line and an operator_review gate.
    AgendaSource.SCOUT: 0.35,
}


@dataclass(frozen=True)
class AgendaWeights:
    """Tunable scoring weights — operator-edited via agenda.yaml."""

    operator_priority_weight: float = 50.0
    age_weight: float = 1.0
    risk_weight: float = -10.0
    budget_remaining_weight: float = 5.0
    source_trust_weight: float = 8.0
    # Task 2B — agent-vetter usefulness signal. Default 0.0 keeps scoring
    # unchanged until agenda.yaml::scoring.vet_weight is configured.
    vet_weight: float = 0.0
    age_cap_hours: float = DEFAULT_AGE_CAP_HOURS
    source_trust: dict[AgendaSource, float] = field(
        default_factory=lambda: dict(_DEFAULT_SOURCE_TRUST)
    )


def _age_hours(item: AgendaItem, now: datetime) -> float:
    created = item.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    delta = (now - created).total_seconds() / 3600.0
    return max(0.0, delta)


def _budget_remaining_ratio(item: AgendaItem) -> float:
    """0.0 = no headroom; 1.0 = full headroom (or unspecified cap)."""
    tokens_cap = item.budget_tokens_cap
    seconds_cap = item.budget_seconds_cap
    if tokens_cap == 0 and seconds_cap == 0:
        return 1.0
    tokens_remaining = 1.0
    seconds_remaining = 1.0
    if tokens_cap > 0:
        tokens_remaining = max(0.0, 1.0 - item.budget_tokens_spent / tokens_cap)
    if seconds_cap > 0:
        seconds_remaining = max(0.0, 1.0 - item.budget_seconds_spent / seconds_cap)
    return min(tokens_remaining, seconds_remaining)


def score_item(
    item: AgendaItem,
    weights: AgendaWeights,
    *,
    now: datetime | None = None,
) -> tuple[float, dict[str, float]]:
    """Compute ``(priority_score, components)`` for ``item``.

    Each component value is the raw signal multiplied by its weight;
    their sum equals ``priority_score`` exactly."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    age_hours = _age_hours(item, moment)
    age_capped = min(age_hours, weights.age_cap_hours)
    risk = _RISK_SCORE.get(item.risk_class, 1e9)
    budget_ratio = _budget_remaining_ratio(item)
    trust = weights.source_trust.get(item.source, 0.0)

    components = {
        "operator_priority": weights.operator_priority_weight * float(item.operator_priority),
        "age": weights.age_weight * age_capped,
        "risk": weights.risk_weight * risk,
        "budget_remaining": weights.budget_remaining_weight * budget_ratio,
        "source_trust": weights.source_trust_weight * trust,
        "vet": weights.vet_weight * item.vet_score,
    }
    total = sum(components.values())
    return total, components


__all__ = [
    "AgendaWeights",
    "DEFAULT_AGE_CAP_HOURS",
    "score_item",
]
