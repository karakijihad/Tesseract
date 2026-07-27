"""Scout discovery (P7 Task 2b) → AgendaItemDraft.

``ScoutJob`` publishes one ``AutonomyEvent(source=SCOUT)`` per evaluated
candidate that clears its identity-anchored bar (query-gen → sweep → dedup
→ pre-filter → evaluation). Always propose-class: ``SCOUT`` sits in
``agenda.yaml::vetter.vet_required`` so the item mints ``UNVETTED`` first,
and — mirroring ``repo_upgrade``'s / ``strategist``'s unconditional gate —
every draft also carries an ``operator_review`` approval gate so a
vetter-promoted item still parks at ``AWAITING_OPERATOR`` instead of
dispatching. Plan text: "propose-don't-act" — no auto-apply, ever, even
for a model-swap finding (the diff rides as TEXT in the rationale).
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.models import AgendaSource, ApprovalGate, RiskClass


def map(event: AutonomyEvent) -> list[AgendaItemDraft]:
    payload = event.payload or {}
    title = str(payload.get("title") or "").strip()
    url = str(payload.get("url") or "").strip()
    why_us_why_now = str(payload.get("why_us_why_now") or "").strip()
    if not title or not url or not why_us_why_now:
        return []
    diff_text = str(payload.get("diff_text") or "").strip()

    goal = f"scout finding: {title}"[:500]
    rationale_parts = [why_us_why_now]
    if diff_text:
        rationale_parts.append(f"proposed diff:\n{diff_text}")
    rationale_parts.append(f"source: {url}")
    rationale = " | ".join(rationale_parts)[:2000]

    gate = ApprovalGate(kind="operator_review", target=f"scout:{title[:60]}")

    return [
        AgendaItemDraft(
            goal=goal,
            source=AgendaSource.SCOUT,
            risk_class=RiskClass.PROPOSE,
            source_event_id=event.event_id,
            rationale=rationale,
            approvals_required=(gate,),
            slug=f"scout-{title[:30]}",
        )
    ]


__all__ = ["map"]
