"""AgendaItem Pydantic v2 model — the schema is locked; extend deliberately."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tesseract.orchestrator.workers.record import RiskClass


class AgendaSource(str, Enum):
    OPERATOR = "operator"
    OPERATOR_VIEW = "operator_view"
    MISSION_REFLECTION = "mission_reflection"  # historical records only — mission engine deleted
    PROVIDER_WATCH = "provider_watch"
    REPO_HEALTH = "repo_health"  # historical records only — mapper deleted P4 prune wave 2
    MEMORY_SIGNAL = "memory_signal"  # historical records only — mapper deleted P4 prune wave 2
    VAULT_SIGNAL = "vault_signal"
    RECOVERY = "recovery"
    SELF_REFLECTION = "self_reflection"
    STRATEGIST = "strategist"
    REPO_UPGRADE = "repo_upgrade"
    SCOUT = "scout"


class AgendaStatus(str, Enum):
    UNVETTED = "unvetted"
    PROPOSED = "proposed"
    SELECTED = "selected"
    RUNNING = "running"
    AWAITING_OPERATOR = "awaiting_operator"
    RESUME_QUEUED = "resume_queued"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


TERMINAL_STATUSES = frozenset(
    {
        AgendaStatus.DONE,
        AgendaStatus.CANCELLED,
        AgendaStatus.ABANDONED,
        AgendaStatus.SUPERSEDED,
    }
)


ApprovalKind = Literal[
    "config_apply",
    "kernel_patch",
    "dependency_install",
    "domain_egress",
    "operator_review",
]

ArtifactKind = Literal[
    "mission",
    "worker_log",
    "reflection",
    "diff",
    "vault_doc",
    "memory_entry",
]

TransitionActor = Literal["kernel", "governor", "operator", "recovery"]


class ApprovalGate(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    kind: ApprovalKind
    target: str
    fulfilled: bool = False
    fulfilled_at: datetime | None = None
    fulfilled_by: str | None = None


class StatusTransition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_status: AgendaStatus | None
    to_status: AgendaStatus
    at: datetime
    reason: str = ""
    by: TransitionActor = "kernel"


class ArtifactRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ArtifactKind
    path: str
    summary: str = ""


class AgendaItem(BaseModel):
    """Per ``_shared/agenda-item-schema.md``. Mutates in place; caller
    follows every state change with ``AgendaStore.save`` for the rewrite
    to hit disk."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    # Identity
    id: str
    created_at: datetime
    updated_at: datetime
    source: AgendaSource
    source_event_id: str | None = None

    # Intent
    goal: str = Field(max_length=500)
    rationale: str = Field(default="", max_length=2000)
    risk_class: RiskClass
    approvals_required: list[ApprovalGate] = Field(default_factory=list)

    # Scoring
    priority_score: float = 0.0
    score_components: dict[str, float] = Field(default_factory=dict)
    score_computed_at: datetime | None = None
    # Task 2B — agent-vetter usefulness score (0-1), set by
    # AutonomyVetterJob on PROMOTE before the item leaves UNVETTED.
    # Folded into priority_score via AgendaWeights.vet_weight.
    vet_score: float = Field(default=0.0, ge=0.0, le=1.0)

    # Budget
    budget_tokens_cap: int = 0
    budget_seconds_cap: int = 0
    budget_tokens_spent: int = 0
    budget_seconds_spent: int = 0

    # Lifecycle
    status: AgendaStatus = AgendaStatus.PROPOSED
    status_history: list[StatusTransition] = Field(default_factory=list)
    blocked_reason: str | None = None
    last_decision: str | None = None

    # Linked work
    linked_missions: list[str] = Field(default_factory=list)
    linked_workers: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)

    # Operator interaction
    operator_priority: int = Field(default=0, ge=-2, le=5)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def transition_to(
        self,
        new_status: AgendaStatus,
        *,
        reason: str = "",
        by: TransitionActor = "kernel",
    ) -> None:
        """Append a transition entry, bump ``updated_at``, set ``status``."""
        if new_status == self.status:
            return
        now = datetime.now(timezone.utc)
        self.status_history.append(
            StatusTransition(
                from_status=self.status,
                to_status=new_status,
                at=now,
                reason=reason,
                by=by,
            )
        )
        self.status = new_status
        self.updated_at = now


def mint_agenda_id(slug: str, *, now: datetime | None = None) -> str:
    """Return ``ag-YYYY-MM-DD-HHMM-<slug>`` from a kebab-cased goal fragment."""
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = when.strftime("%Y-%m-%d-%H%M")
    cleaned = "".join(c if c.isalnum() or c == "-" else "-" for c in slug.lower())
    cleaned = "-".join(filter(None, cleaned.split("-")))[:40]
    if not cleaned:
        cleaned = "item"
    return f"ag-{stamp}-{cleaned}"


def dedupe_key(goal: str, source: AgendaSource) -> str:
    """Stable key used to detect duplicate proposals from the same source.

    Lowercased + whitespace-normalised goal joined with the source enum.
    Two operator-typed items with identical text dedupe; two observer
    suggestions with identical phrasing dedupe; an operator item and an
    observer item with identical text are NOT deduped (different source
    trust)."""
    normalised = " ".join(goal.lower().split())
    return f"{source.value}::{normalised}"


__all__ = [
    "AgendaItem",
    "AgendaSource",
    "AgendaStatus",
    "ApprovalGate",
    "ApprovalKind",
    "ArtifactKind",
    "ArtifactRef",
    "RiskClass",
    "StatusTransition",
    "TERMINAL_STATUSES",
    "TransitionActor",
    "dedupe_key",
    "mint_agenda_id",
]
