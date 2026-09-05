"""AgendaItem Pydantic v2 model — the schema is locked; extend deliberately."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tesseract.orchestrator.outcome import RunOutcome
from tesseract.orchestrator.workers.record import RiskClass

# The version a record written by this code carries. A record with none was
# written before the obligation contract existed and loads with the contract
# fields empty, because it never had anything in them to lose. A record that
# CLAIMS this version and lacks one of them is broken and does not load. Bump
# it when a field changes meaning or shape, never for an addition with a
# default, which the legacy path already absorbs.
AGENDA_SCHEMA_VERSION = 1

# What the current version requires on disk. Declared once so the loader and
# the tests read the same list.
CONTRACT_FIELDS = (
    "success_criteria",
    "verification",
    "turn_ids",
    "attempts",
    "current_checkpoint",
)


class AgendaSource(str, Enum):
    # Live sources. A value here has a mapper AND a producer; the pairing is
    # declared in `mappers/__init__.py::SOURCE_PRODUCERS` and checked at boot.
    OPERATOR = "operator"
    PROVIDER_WATCH = "provider_watch"
    # The two below are written straight to the store, so they have no mapper
    # and no entry in `SOURCE_PRODUCERS`, which pairs mappers with publishers
    # and would fail the boot check if given a source with neither.
    #
    # `recovery` is written by `recovery.py` at the moment a circuit breaker
    # turns away work that nothing else will retry. There is no bus event
    # because the only subscribers are open websockets, and the case this
    # exists for is nobody being at the screen.
    RECOVERY = "recovery"
    # Written straight to the store by `follow_up_mapper` when a finished
    # delegation's summary reads as actionable. No bus event and so no mapper —
    # it names what produced the item, which is what a source is for. It rode
    # `self_reflection` until that source was deleted, which would have left the
    # one live follow-up path labelled with a retired heartbeat's name.
    FOLLOW_UP = "follow_up"
    # Written straight to the store by `task_propose`, and by nothing else:
    # the assistant proposed it in a conversation and the operator accepted it
    # at the tool's gate, so the acceptance is the operator's and the row says
    # so. No mapper and no bus event. A task is worked in the conversation
    # that made it, so the kernel does not dispatch this source to a worker
    # and the reaper does not abandon it: it is owed until it is verified,
    # failed, or the operator cancels it.
    TASK = "task"

    # Historical records only — the mapper and the producer are both gone, and
    # the value survives so an archived item written under it still loads. A
    # value in this block must never gain a mapper: reviving one means deciding
    # again, not un-deleting.
    OPERATOR_VIEW = "operator_view"  # ambient presence telemetry
    MISSION_REFLECTION = "mission_reflection"  # the mission engine
    REPO_HEALTH = "repo_health"
    MEMORY_SIGNAL = "memory_signal"
    VAULT_SIGNAL = "vault_signal"  # never had a publisher at all
    REPO_UPGRADE = "repo_upgrade"  # proposed changes to the sealed app tree
    SCOUT = "scout"  # same
    SELF_REFLECTION = "self_reflection"  # a model asked what might be worth doing
    STRATEGIST = "strategist"  # same, on a longer horizon


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
    # The work was tried and cannot continue, and there is no decision left to
    # ask the operator for. Carries its reason on the transition and what was
    # tried in `attempts`. Never `done`, which is evidenced, and never
    # `abandoned`, which is the reaper's word for work nobody came back to.
    FAILED = "failed"


TERMINAL_STATUSES = frozenset(
    {
        AgendaStatus.DONE,
        AgendaStatus.CANCELLED,
        AgendaStatus.ABANDONED,
        AgendaStatus.SUPERSEDED,
        AgendaStatus.FAILED,
    }
)


class UnverifiedDone(ValueError):
    """An item that said what evidence would end it was closed without any."""


class UnexplainedFailure(ValueError):
    """`failed` was written with no reason, which is the silence it exists to end."""


class ObligationFrozen(ValueError):
    """`goal` or `success_criteria` changed after the work began, and not by an
    operator amendment."""


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


class Attempt(BaseModel):
    """One try at the work: which turn made it and how it ended.

    The classification is `RunOutcome`, the one vocabulary every run already
    ends on, so an attempt and the turn it names cannot disagree about what
    happened. `orchestrator/turns/tasks.py::note_turn_ended` appends one when
    a turn on the task ends, once per turn. Nothing retries a task on its own:
    after a restart it waits in `resume_queued` until a conversation takes it
    up, so the list grows only as fast as people work it. A task worked across
    many turns on purpose is a record, not a loop.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: str = ""
    started_at: datetime
    ended_at: datetime | None = None
    outcome: RunOutcome
    reason: str = ""


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
    # Historical. A vetting job set this 0-1 usefulness score on promotion out
    # of UNVETTED; both the job and the weight that read this are gone. Kept as
    # a field so an item written while it existed still loads.
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

    # The obligation contract. An item is owed until it is verified, and these
    # are what make that a fact on disk rather than a status somebody wrote.
    # Invariants, held together:
    #   1. `success_criteria` says what observable evidence would end this.
    #      Empty means nothing was declared, which is what every background
    #      item says today; `done` then means what it always meant.
    #   2. An item WITH criteria cannot reach `done` while `verification` is
    #      empty (`transition_to` refuses). A closing sentence is not evidence.
    #   3. `failed` always carries a reason (`transition_to` refuses without).
    #   4. Once the work has run, `goal` and `success_criteria` change only
    #      through `amend`, which is the operator's and leaves a row in the
    #      history. The store enforces it against the record on disk, because
    #      an in-memory model cannot see what it used to say.
    #   5. `turn_ids` is execution evidence, written from the turn's side too
    #      (`RunManifest.task_id`), so neither record is authoritative alone.
    #   6. `attempts` is bounded by whoever appends, never by the model.
    #   7. `current_checkpoint` is a reference to a record with an owner, never
    #      a copy. Declared here; written by the checkpoint store when it exists.
    success_criteria: str = Field(default="", max_length=2000)
    verification: str = Field(default="", max_length=4000)
    # Who wrote `verification`: "gate" when the project's verify steps ran
    # and this is their rendered result, "model" when it is the assistant's
    # own sentence, "" when nothing has been written. A learner that cannot
    # tell these apart learns from what was said; both live outside
    # CONTRACT_FIELDS so a record written before they existed still loads.
    verification_by: Literal["", "gate", "model"] = ""
    # The project a task belongs to (`projects/registry.json` id). Empty is
    # a task with no project, which closes on the assistant's word and says
    # so in `verification_by`.
    project_id: str = ""
    turn_ids: list[str] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    current_checkpoint: str | None = None
    schema_version: int = AGENDA_SCHEMA_VERSION

    @model_validator(mode="before")
    @classmethod
    def _load_by_version(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        version = data.get("schema_version")
        if version is None:
            return {**data, "schema_version": AGENDA_SCHEMA_VERSION}
        if not isinstance(version, int) or version > AGENDA_SCHEMA_VERSION:
            raise ValueError(
                f"agenda item {data.get('id')!r} was written by a newer version "
                f"of this app (schema {version!r}, this one reads up to "
                f"{AGENDA_SCHEMA_VERSION}). Update the app before opening it."
            )
        missing = [name for name in CONTRACT_FIELDS if name not in data]
        if missing:
            raise ValueError(
                f"agenda item {data.get('id')!r} claims schema {version} and "
                f"is missing {missing}. The record is damaged; nothing here can "
                f"repair it, so restore it from a copy or cancel it."
            )
        return data

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def has_run(self) -> bool:
        """Whether the work has begun, which is when what it owes freezes."""
        return self.status == AgendaStatus.RUNNING or any(
            t.to_status == AgendaStatus.RUNNING for t in self.status_history
        )

    def amend(
        self,
        *,
        goal: str | None = None,
        success_criteria: str | None = None,
        reason: str,
        by: TransitionActor = "operator",
    ) -> None:
        """Change what the item owes, on the record. Only the operator may, and
        the change is a row in the history so it can be read back later. A
        same-status row is what marks it: `transition_to` never writes one."""
        if by != "operator":
            raise ObligationFrozen(
                f"agenda item {self.id!r}: only the operator may change what a "
                f"task owes; {by!r} tried"
            )
        if not reason.strip():
            raise ObligationFrozen(
                f"agenda item {self.id!r}: an amendment needs a reason"
            )
        now = datetime.now(timezone.utc)
        if goal is not None:
            self.goal = goal
        if success_criteria is not None:
            self.success_criteria = success_criteria
        self.status_history.append(
            StatusTransition(
                from_status=self.status,
                to_status=self.status,
                at=now,
                reason=f"amended: {reason.strip()}",
                by=by,
            )
        )
        self.updated_at = now

    def amended_since(self, rows_before: int) -> bool:
        """Whether an operator amendment row was appended after `rows_before`
        history rows existed. The store's freeze check reads it."""
        return any(
            t.from_status == t.to_status and t.by == "operator"
            for t in self.status_history[rows_before:]
        )

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
        if (
            new_status == AgendaStatus.DONE
            and self.success_criteria.strip()
            and not self.verification.strip()
        ):
            raise UnverifiedDone(
                f"agenda item {self.id!r} said what would count as done and "
                f"has no evidence of it. Record the evidence in `verification` "
                f"first, or close it as `failed` with the reason."
            )
        if new_status == AgendaStatus.FAILED and not reason.strip():
            raise UnexplainedFailure(
                f"agenda item {self.id!r} cannot fail without a reason. Say "
                f"what stopped it."
            )
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
    "AGENDA_SCHEMA_VERSION",
    "AgendaItem",
    "AgendaSource",
    "AgendaStatus",
    "ApprovalGate",
    "ApprovalKind",
    "ArtifactKind",
    "ArtifactRef",
    "Attempt",
    "CONTRACT_FIELDS",
    "ObligationFrozen",
    "RiskClass",
    "StatusTransition",
    "TERMINAL_STATUSES",
    "TransitionActor",
    "UnexplainedFailure",
    "UnverifiedDone",
    "dedupe_key",
    "mint_agenda_id",
]
