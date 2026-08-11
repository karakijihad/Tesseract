"""operator_view events → AgendaItemDraft.

The WS handler in :mod:`tesseract.mirror.server.routes.operator_view`
publishes one event per ``view_snapshot`` envelope. Most are ambient
context — the mapper drains them as noise.

**2026-05-19 demotion (codex audit-2 P1 #2 / audit-1 P1 #4):**
``repeat_switch`` no longer emits an agenda candidate by itself. The
signal still lives in ``app[PRESENCE_KEY]`` and is exposed via
``GET /api/operator/presence``; a future digest layer can consume it
without spamming the agenda surface. Prior behaviour generated a fresh
"propose this view as the default route" item every time the operator
flipped tabs more than 3 times — clutter, not strategy. The audit-
flagged live system had `view-switch-chat` at switch counts 3 → 9,
all dispatched.

Two remaining triggers keep producing candidates:

- ``long_dwell=True``     — operator sat on the same view for ≥5 min
                            (paired with operational signal: summarise
                            what they were looking at).
- ``repeat_switch=True`` + ``paired_with_failure=True`` — the WS
                            handler can stamp this when the target view
                            is showing real operational pain (e.g.
                            recovery with failures, blocked approvals
                            unresolved). Only then does repeat-switch
                            mean "this is a problem", not "operator
                            likes this tab".

All candidates carry ``RiskClass.PROPOSE``: the operator approves
before any UI / config change.
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass


def _is_true(value: object) -> bool:
    return value is True


def map(event: AutonomyEvent) -> list[AgendaItemDraft]:
    payload = event.payload or {}
    view = str(payload.get("view") or "").strip()
    if not view:
        return []
    long_dwell = _is_true(payload.get("long_dwell"))
    repeat_switch = _is_true(payload.get("repeat_switch"))
    paired_with_failure = _is_true(payload.get("paired_with_failure"))
    # repeat_switch alone is no longer enough — must pair with an
    # operational consequence (audit-2 P1 #2). Bare repeat_switch
    # signals stay observable via /api/operator/presence.
    repeat_switch_actionable = repeat_switch and paired_with_failure
    if not (long_dwell or repeat_switch_actionable):
        return []

    drafts: list[AgendaItemDraft] = []
    if long_dwell:
        dwell_seconds = float(payload.get("dwell_seconds") or 0.0)
        prev_view = str(payload.get("prev_view") or "").strip() or "(unknown)"
        # The goal is an identity, not a reading. Baking the dwell into it
        # made every emission a unique string, so the exact-goal dedupe could
        # never match and five near-identical items reached the live agenda.
        # The measurement belongs in `rationale`, which already carries it.
        goal = f"summarise recent activity on the '{prev_view}' view"
        rationale = (
            f"long_dwell trigger — prev_view={prev_view} dwell={dwell_seconds:.1f}s "
            f"now_on={view}"
        )
        drafts.append(
            AgendaItemDraft(
                goal=goal[:500],
                source=AgendaSource.OPERATOR_VIEW,
                risk_class=RiskClass.PROPOSE,
                source_event_id=f"{event.event_id}:long_dwell",
                rationale=rationale[:2000],
                slug=f"view-dwell-{prev_view[:30]}",
            )
        )
    if repeat_switch_actionable:
        switch_count = int(payload.get("switch_count_today") or 0)
        failure_summary = str(payload.get("failure_summary") or "").strip()
        # Same rule as the dwell goal above: `switch_count` climbs on every
        # emission, so embedding it would defeat the exact-goal dedupe the
        # same way. It is already in the rationale below.
        goal = f"address ongoing issue on '{view}'"
        rationale_parts = [
            f"repeat_switch+failure trigger — view={view}",
            f"switch_count_today={switch_count}",
        ]
        if failure_summary:
            rationale_parts.append(f"failure: {failure_summary[:400]}")
        rationale = " | ".join(rationale_parts)[:2000]
        drafts.append(
            AgendaItemDraft(
                goal=goal[:500],
                source=AgendaSource.OPERATOR_VIEW,
                risk_class=RiskClass.PROPOSE,
                source_event_id=f"{event.event_id}:repeat_switch_failure",
                rationale=rationale,
                slug=f"view-issue-{view[:30]}",
            )
        )
    return drafts


__all__ = ["map"]
