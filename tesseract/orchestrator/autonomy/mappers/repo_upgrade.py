"""Repo-upgrade research (P7 Task 2) → AgendaItemDraft.

``RepoUpgradeResearchJob`` publishes one ``AutonomyEvent(source=REPO_UPGRADE)``
per target after a read-only Codex research pass (outdated deps, upstream
releases, applicable improvements — including self-improvement proposals
against the assistant's own agent cards/rules). Always propose-class: the plan is
explicit that nothing here auto-applies, and ``REPO_UPGRADE`` sits in
``agenda.yaml::vetter.vet_required`` so the item mints ``UNVETTED`` and
waits on ``AutonomyVetterJob`` before it is even selectable.

Every draft also carries an unconditional ``operator_review`` approval
gate — mirroring ``strategist.py``'s pattern (kernel.py's admission gate
never dispatches a ``PROPOSED`` item with an unfulfilled approval; see
``_approvals_satisfied``). Plan text: "one agenda proposal ... for operator
review. No auto-apply." — the gate is what makes that true even after the
vetter promotes the item out of ``UNVETTED``.

The goal is built from ``payload["summary"]`` — the job's distilled,
preamble-free one-line proposal (see ``repo_upgrade_research._distill_summary``)
— not the raw ``findings`` blob, so an obedience preamble/instruction echo
from Codex can never become the goal text. Falls back to a truncated
``findings`` slice only for events published before the ``summary`` field
existed.
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.models import AgendaSource, ApprovalGate, RiskClass


def map(event: AutonomyEvent) -> list[AgendaItemDraft]:
    payload = event.payload or {}
    path = str(payload.get("path") or "").strip()
    findings = str(payload.get("findings") or "").strip()
    if not path or not findings:
        return []
    focus = str(payload.get("focus") or "").strip()
    summary = str(payload.get("summary") or "").strip() or findings[:200]

    goal = f"review repo-upgrade findings for {path}: {summary}"
    rationale_parts = [f"repo_upgrade_research findings for {path}"]
    if focus:
        rationale_parts.append(f"focus={focus}")
    rationale_parts.append(findings[:1500])
    rationale = " | ".join(rationale_parts)[:2000]

    gate = ApprovalGate(kind="operator_review", target=f"repo_upgrade:{path[:60]}")

    return [
        AgendaItemDraft(
            goal=goal[:500],
            source=AgendaSource.REPO_UPGRADE,
            risk_class=RiskClass.PROPOSE,
            source_event_id=event.event_id,
            rationale=rationale,
            approvals_required=(gate,),
            slug=f"repo-upgrade-{path[:30]}",
        )
    ]


__all__ = ["map"]
