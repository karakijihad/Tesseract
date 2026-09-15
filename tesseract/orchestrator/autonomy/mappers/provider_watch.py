"""Provider watch hits → AgendaItemDraft.

Where the probe substrate writes
``<TESSERACT_HOME>/logs/provider-health/*.jsonl``, the provider-watch
publisher reads the rolling window and emits one
:class:`AutonomyEvent` per drift event. The narrower input is the
``provider_watch`` scheduler job's digest — the kernel reads its
job-done payload when it carries a ``new_models`` or
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
    density = payload.get("schema_density") or []
    drafts = _density_drafts(event, density)
    if not (new_models or deprecated or failures):
        return drafts
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
    ] + drafts


def _density_drafts(event: AutonomyEvent, density: list) -> list[AgendaItemDraft]:
    """A model whose tool schemas now cost a different amount than the catalog
    says. The check never rewrites a figure that is there, so this card is how
    the new reading reaches the file: the operator applies it or leaves it."""
    rows = [d for d in density if isinstance(d, dict) and d.get("ref")]
    if not rows:
        return []
    detail = "; ".join(
        f"{d['ref']}: catalog says {d.get('declared')}, measured {d.get('measured')}"
        for d in rows[:5]
    )
    return [
        AgendaItemDraft(
            goal=(
                "tool schemas cost a different amount than providers.yaml says: "
                + detail
            )[:500],
            source=AgendaSource.PROVIDER_WATCH,
            risk_class=RiskClass.PROPOSE,
            source_event_id=event.event_id,
            rationale=(
                "The request cost every surface shows is priced with the catalog "
                "figure, so it reads off by the difference until the figure is "
                f"updated. {detail}"
            )[:2000],
            approvals_required=(
                ApprovalGate(
                    kind="config_apply",
                    target="tesseract/config/providers.yaml",
                    fulfilled=False,
                ),
            ),
            slug=f"schema-density-{rows[0]['ref'][:30]}",
        )
    ]


__all__ = ["map"]
