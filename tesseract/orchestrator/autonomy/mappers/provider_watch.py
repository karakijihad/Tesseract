"""Provider watch hits → AgendaItemDraft.

AU-5 cross-ref (per ``Docs/Plan/autonomy/phase-AU-5`` §1): once AU-14
ships the probe substrate and writes
``<TESSERACT_HOME>/logs/provider-health/*.jsonl``, the provider-watch
publisher reads the rolling window and emits one
:class:`AutonomyEvent` per drift event. Today's narrower input is the
existing ``provider_watch`` scheduler job's digest — the kernel reads
its job-done payload when it carries a ``new_models`` or
``deprecated_models`` list.

The draft is always ``propose`` — provider swaps affect role wiring
and the operator must approve before any ``roles.yaml`` mutation
lands.
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
    payload = event.payload
    new_models = payload.get("new_models") or []
    deprecated = payload.get("deprecated_models") or []
    failures = payload.get("failures") or []
    if not (new_models or deprecated or failures):
        return []
    parts = []
    if new_models:
        parts.append(f"new={list(new_models)[:3]}")
    if deprecated:
        parts.append(f"deprecated={list(deprecated)[:3]}")
    if failures:
        parts.append(f"failing={list(failures)[:3]}")
    summary = " ".join(parts)
    goal = f"review provider watch drift: {summary[:200]}"
    return [
        AgendaItemDraft(
            goal=goal[:500],
            source=AgendaSource.PROVIDER_WATCH,
            risk_class=RiskClass.PROPOSE,
            source_event_id=event.event_id,
            rationale=summary[:2000],
            approvals_required=(
                ApprovalGate(
                    kind="config_apply",
                    target="tesseract/config/roles.yaml",
                    fulfilled=False,
                ),
            ),
            slug=f"provider-watch-{summary[:30]}",
        )
    ]


__all__ = ["map"]
