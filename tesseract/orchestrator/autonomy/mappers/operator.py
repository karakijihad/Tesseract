"""Operator → AgendaItemDraft.

Operator-authored items flow in via ``POST /api/agenda/items`` (AU-4
S2 routes) — they do not pass through the event bus today; this mapper
exists for completeness so an operator entry point that publishes
``AgendaSource.OPERATOR`` to the bus (e.g. a future Telegram inbound
command) yields the same draft shape.

Required payload keys: ``goal``. Optional: ``rationale``,
``risk_class`` (defaults to ``propose`` — operator-typed asks are
operator-attended by construction), ``operator_priority``,
``budget_tokens_cap``, ``budget_seconds_cap``.
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass


def map(event: AutonomyEvent) -> list[AgendaItemDraft]:
    goal = (event.payload.get("goal") or "").strip()
    if not goal:
        return []
    risk_raw = event.payload.get("risk_class", RiskClass.PROPOSE.value)
    try:
        risk = RiskClass(risk_raw)
    except ValueError:
        risk = RiskClass.PROPOSE
    if risk == RiskClass.ABSOLUTE_DENY:
        return []
    return [
        AgendaItemDraft(
            goal=goal[:500],
            source=AgendaSource.OPERATOR,
            risk_class=risk,
            source_event_id=event.event_id,
            rationale=str(event.payload.get("rationale", ""))[:2000],
            operator_priority=int(event.payload.get("operator_priority", 0)),
            budget_tokens_cap=int(event.payload.get("budget_tokens_cap", 0)),
            budget_seconds_cap=int(event.payload.get("budget_seconds_cap", 0)),
            slug=event.payload.get("slug") or goal[:40],
        )
    ]


__all__ = ["map"]
