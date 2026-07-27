"""Per-source mapper unit tests. Mappers are pure functions —
``map(event) -> list[AgendaItemDraft]`` — so these run without any
fixtures."""

from __future__ import annotations

from tesseract.orchestrator.autonomy import AutonomyEvent
from tesseract.orchestrator.autonomy.mappers import (
    map_operator,
    map_provider_watch,
)
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass


def test_operator_mapper_emits_propose_draft() -> None:
    event = AutonomyEvent.make(
        AgendaSource.OPERATOR,
        {"goal": "research the TARS soul layer", "operator_priority": 2},
    )
    drafts = map_operator(event)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.source == AgendaSource.OPERATOR
    assert draft.risk_class == RiskClass.PROPOSE
    assert draft.operator_priority == 2
    assert draft.source_event_id == event.event_id


def test_operator_mapper_drops_empty_goal() -> None:
    assert map_operator(AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": ""})) == []
    assert map_operator(AutonomyEvent.make(AgendaSource.OPERATOR, {})) == []


def test_operator_mapper_refuses_absolute_deny() -> None:
    drafts = map_operator(
        AutonomyEvent.make(
            AgendaSource.OPERATOR,
            {"goal": "do thing", "risk_class": "absolute_deny"},
        )
    )
    assert drafts == []


def test_provider_watch_mapper_no_drift_drops() -> None:
    no_drift = AutonomyEvent.make(
        AgendaSource.PROVIDER_WATCH,
        {"new_models": [], "deprecated_models": [], "failures": []},
    )
    assert map_provider_watch(no_drift) == []
    drift = AutonomyEvent.make(
        AgendaSource.PROVIDER_WATCH,
        {"new_models": ["gpt-x"], "deprecated_models": [], "failures": []},
    )
    drafts = map_provider_watch(drift)
    assert len(drafts) == 1
    assert drafts[0].risk_class == RiskClass.PROPOSE
    assert drafts[0].approvals_required[0].target == "tesseract/config/roles.yaml"


