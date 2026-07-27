"""AU-23 — strategist event → AgendaItemDraft mapper."""

from __future__ import annotations

from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.mappers import DEFAULT_MAPPERS
from tesseract.orchestrator.autonomy.mappers.strategist import map as map_strategist
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass


def _ev(payload: dict, event_id: str = "evt_test") -> AutonomyEvent:
    return AutonomyEvent.make(AgendaSource.STRATEGIST, payload, event_id=event_id)


def test_mapper_registered_in_defaults():
    assert AgendaSource.STRATEGIST in DEFAULT_MAPPERS
    assert DEFAULT_MAPPERS[AgendaSource.STRATEGIST] is map_strategist


def test_mapper_emits_propose_with_operator_review_gate():
    drafts = map_strategist(_ev({
        "slug": "pickup-anthropic-docs",
        "goal": "Ingest the new Anthropic SDK 0.45.x docs and refresh the wiki.",
        "rationale": "Two worker failures traced to outdated SDK calls.",
        "success_criteria": ["wiki page mentions 0.45", "no SDK-related failures"],
        "suggested_risk_class": "propose",
        "evidence": ["ag-2026-05-19-1700-foo", "leaf-abc"],
        "confidence": 0.78,
        "horizon_days": 5,
    }))
    assert len(drafts) == 1
    d = drafts[0]
    assert d.source is AgendaSource.STRATEGIST
    assert d.risk_class is RiskClass.PROPOSE
    assert d.slug == "strategist-pickup-anthropic-docs"
    assert d.source_event_id == "evt_test"
    assert len(d.approvals_required) == 1
    gate = d.approvals_required[0]
    assert gate.kind == "operator_review"
    assert gate.target == "strategist:pickup-anthropic-docs"
    assert "ingest" in d.goal.lower()
    assert "confidence=0.78" in d.rationale
    assert "horizon=5d" in d.rationale
    assert "success:" in d.rationale


def test_mapper_drops_empty_goal():
    assert map_strategist(_ev({"goal": ""})) == []
    assert map_strategist(_ev({})) == []


def test_mapper_coerces_autonomous_to_propose():
    drafts = map_strategist(_ev({
        "slug": "auto",
        "goal": "Do a thing.",
        "rationale": "x",
        "success_criteria": ["ok"],
        "suggested_risk_class": "autonomous",
        "confidence": 0.7,
        "horizon_days": 3,
    }))
    assert drafts[0].risk_class is RiskClass.PROPOSE


def test_mapper_coerces_absolute_deny_to_propose():
    drafts = map_strategist(_ev({
        "slug": "deny",
        "goal": "Do a thing.",
        "rationale": "x",
        "success_criteria": ["ok"],
        "suggested_risk_class": "absolute_deny",
        "confidence": 0.7,
        "horizon_days": 3,
    }))
    assert drafts[0].risk_class is RiskClass.PROPOSE


def test_mapper_keeps_operator_gate():
    drafts = map_strategist(_ev({
        "slug": "gate",
        "goal": "Do a thing.",
        "rationale": "x",
        "success_criteria": ["ok"],
        "suggested_risk_class": "operator_gate",
        "confidence": 0.7,
        "horizon_days": 3,
    }))
    assert drafts[0].risk_class is RiskClass.OPERATOR_GATE


def test_mapper_handles_missing_optional_fields():
    drafts = map_strategist(_ev({
        "goal": "Do a thing without much metadata.",
    }))
    assert len(drafts) == 1
    d = drafts[0]
    # No rationale, no criteria, no evidence — still a valid draft.
    assert d.risk_class is RiskClass.PROPOSE
    assert d.slug == "strategist-strategist"
