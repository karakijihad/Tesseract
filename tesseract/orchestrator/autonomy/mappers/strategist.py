"""Strategist (AU-23 weekly initiative curator) → AgendaItemDraft.

Each accepted ``Initiative`` published by ``AutonomyStrategistJob``
becomes one agenda item via this mapper. The strategist is by design
operator-attended: every draft attaches an ``operator_review`` gate
regardless of the underlying risk class, so the operator decides
whether the kernel dispatches it.

The mapper is pure (no IO). Risk class is taken from the payload's
``suggested_risk_class`` (PROPOSE or OPERATOR_GATE only — the source
module's `_coerce_risk` already pinned AUTONOMOUS/ABSOLUTE_DENY to
PROPOSE before publish).
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.models import (
    AgendaSource,
    ApprovalGate,
    RiskClass,
)


def map(event: AutonomyEvent) -> list[AgendaItemDraft]:
    payload = event.payload or {}
    goal_raw = str(payload.get("goal") or "").strip()
    if not goal_raw:
        return []

    slug = str(payload.get("slug") or "").strip() or "strategist"

    risk_raw = str(payload.get("suggested_risk_class") or "").strip().lower()
    try:
        risk = RiskClass(risk_raw) if risk_raw else RiskClass.PROPOSE
    except ValueError:
        risk = RiskClass.PROPOSE
    if risk is RiskClass.AUTONOMOUS or risk is RiskClass.ABSOLUTE_DENY:
        risk = RiskClass.PROPOSE

    rationale = _compose_rationale(payload)

    gate = ApprovalGate(
        kind="operator_review",
        target=f"strategist:{slug}",
    )

    return [
        AgendaItemDraft(
            goal=goal_raw[:500],
            source=AgendaSource.STRATEGIST,
            risk_class=risk,
            source_event_id=event.event_id,
            rationale=rationale,
            approvals_required=(gate,),
            slug=f"strategist-{slug}",
        )
    ]


def _compose_rationale(payload: dict) -> str:
    """Build the agenda rationale from the strategist's initiative fields.

    Lead with the model's own rationale; append the success criteria + a
    confidence / horizon tail so the dashboard row shows the operator
    everything they need to decide approve / reject inline.
    """
    parts: list[str] = []
    rationale_raw = str(payload.get("rationale") or "").strip()
    if rationale_raw:
        parts.append(rationale_raw[:1200])

    criteria = payload.get("success_criteria") or []
    if isinstance(criteria, list) and criteria:
        criteria_str = "; ".join(str(c).strip() for c in criteria if str(c).strip())
        if criteria_str:
            parts.append(f"success: {criteria_str[:400]}")

    confidence = payload.get("confidence")
    horizon = payload.get("horizon_days")
    tail_bits: list[str] = []
    try:
        if confidence is not None:
            tail_bits.append(f"confidence={float(confidence):.2f}")
    except (TypeError, ValueError):
        pass
    if horizon is not None:
        try:
            tail_bits.append(f"horizon={int(horizon)}d")
        except (TypeError, ValueError):
            pass
    if tail_bits:
        parts.append(" ".join(tail_bits))

    evidence = payload.get("evidence") or []
    if isinstance(evidence, list) and evidence:
        evidence_str = ", ".join(str(e).strip() for e in evidence if str(e).strip())
        if evidence_str:
            parts.append(f"evidence: {evidence_str[:400]}")

    return " | ".join(parts)[:2000]


__all__ = ["map"]
