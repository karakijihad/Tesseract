"""``AgendaItemDraft`` — what mappers return, what the kernel persists.

Mappers are pure functions that transform an ``AutonomyEvent`` into
zero or more ``AgendaItemDraft`` records. The kernel folds the draft
into an :class:`AgendaItem` (minting id, stamping timestamps, copying
risk_class + source) and persists via ``AgendaStore.add``.

Keeping drafts separate from items keeps the mapper signature
stable: a mapper does not need to know about ids, timestamps, or
scoring — only the operator-visible intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    ApprovalGate,
    RiskClass,
    mint_agenda_id,
)


@dataclass(frozen=True)
class AgendaItemDraft:
    """Mapper output. Folded into an :class:`AgendaItem` by the kernel."""

    goal: str
    source: AgendaSource
    risk_class: RiskClass
    source_event_id: str | None = None
    rationale: str = ""
    approvals_required: tuple[ApprovalGate, ...] = ()
    budget_tokens_cap: int = 0
    budget_seconds_cap: int = 0
    operator_priority: int = 0
    slug: str = ""

    def to_item(
        self,
        *,
        now: datetime | None = None,
        status: AgendaStatus = AgendaStatus.PROPOSED,
        budget_defaults: dict[RiskClass, tuple[int, int]] | None = None,
    ) -> AgendaItem:
        """``budget_defaults`` is ``agenda.yaml::budget_defaults``, keyed by
        risk class as ``(tokens_cap, seconds_cap)``. A draft that names its
        own caps keeps them; everything else inherits the class floor. Until
        this was threaded through, no producer set a cap at all, so the
        governor's cost-spiral detector and the scoring term that reads
        remaining headroom were both comparing against zero."""
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        slug = self.slug or self.goal[:40]
        tokens_cap, seconds_cap = (budget_defaults or {}).get(self.risk_class, (0, 0))
        return AgendaItem(
            id=mint_agenda_id(slug, now=moment),
            created_at=moment,
            updated_at=moment,
            source=self.source,
            source_event_id=self.source_event_id,
            goal=self.goal,
            rationale=self.rationale,
            risk_class=self.risk_class,
            approvals_required=list(self.approvals_required),
            budget_tokens_cap=self.budget_tokens_cap or tokens_cap,
            budget_seconds_cap=self.budget_seconds_cap or seconds_cap,
            status=status,
            operator_priority=self.operator_priority,
        )


__all__ = ["AgendaItemDraft"]
