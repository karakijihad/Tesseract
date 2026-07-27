"""Self-reflection (autonomy heartbeat) → AgendaItemDraft.

The AU-20 ``AutonomyHeartbeatJob`` publishes a
``AutonomyEvent(source=SELF_REFLECTION)`` per accepted observation.
Each event becomes at most one agenda candidate, scored deterministically
by AU-4. The mapper itself is pure — risk class comes straight from the
observation's ``suggested_risk_class`` field; ``absolute_deny`` is
filtered out (heartbeat should never propose a hard-banned action).
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass


def map(event: AutonomyEvent) -> list[AgendaItemDraft]:
    payload = event.payload or {}
    observation = str(payload.get("observation") or "").strip()
    if not observation:
        return []
    risk_raw = str(payload.get("suggested_risk_class") or "").strip().lower()
    try:
        risk = RiskClass(risk_raw) if risk_raw else RiskClass.PROPOSE
    except ValueError:
        risk = RiskClass.PROPOSE
    if risk is RiskClass.ABSOLUTE_DENY:
        return []
    raw_evidence = payload.get("evidence_ids")
    evidence = raw_evidence if isinstance(raw_evidence, list) else []
    evidence_str = ", ".join(str(e) for e in evidence if e)
    memory_id = payload.get("memory_id")
    rationale_parts = [f"autonomy_heartbeat observation: {observation[:600]}"]
    if memory_id:
        rationale_parts.append(f"memory_id={memory_id}")
    if evidence_str:
        rationale_parts.append(f"evidence={evidence_str[:800]}")
    rationale = " | ".join(rationale_parts)[:2000]
    goal = f"act on heartbeat observation: {observation[:200]}"
    return [
        AgendaItemDraft(
            goal=goal[:500],
            source=AgendaSource.SELF_REFLECTION,
            risk_class=risk,
            source_event_id=event.event_id,
            rationale=rationale,
            slug=f"heartbeat-{observation[:30]}",
        )
    ]


__all__ = ["map"]
