"""AU-21 mapper — threshold-flagged events → AgendaItemDraft."""

from __future__ import annotations

from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.mappers.operator_view import map as map_view
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass


def _evt(payload: dict) -> AutonomyEvent:
    return AutonomyEvent.make(AgendaSource.OPERATOR_VIEW, payload, event_id="evt-1")


def test_no_threshold_no_draft() -> None:
    drafts = map_view(
        _evt(
            {
                "view": "autonomy",
                "long_dwell": False,
                "repeat_switch": False,
                "dwell_seconds": 10.0,
            }
        )
    )
    assert drafts == []


def test_missing_view_returns_no_drafts() -> None:
    drafts = map_view(_evt({"view": "", "long_dwell": True, "dwell_seconds": 600.0}))
    assert drafts == []


def test_long_dwell_emits_one_propose_draft() -> None:
    drafts = map_view(
        _evt(
            {
                "view": "terminal",
                "prev_view": "recovery",
                "long_dwell": True,
                "repeat_switch": False,
                "dwell_seconds": 360.0,
            }
        )
    )
    assert len(drafts) == 1
    d = drafts[0]
    assert d.source is AgendaSource.OPERATOR_VIEW
    assert d.risk_class is RiskClass.PROPOSE
    assert "recovery" in d.goal
    assert "360" in d.goal
    assert "long_dwell" in d.rationale


def test_bare_repeat_switch_no_longer_emits_draft() -> None:
    """Updated 2026-05-19 (codex audit-2 P1 #2): bare repeat_switch
    was generating "propose this view as the default route" agenda
    spam (view-switch-chat at counts 3 → 9). Demoted — must pair with
    ``paired_with_failure`` to emit."""
    drafts = map_view(
        _evt(
            {
                "view": "schedule",
                "prev_view": "autonomy",
                "long_dwell": False,
                "repeat_switch": True,
                "switch_count_today": 4,
            }
        )
    )
    assert drafts == []


def test_repeat_switch_with_failure_pairing_emits_issue_draft() -> None:
    """The new path — repeat_switch + paired_with_failure signals real
    operational pain (operator returned 4× to a failing view)."""
    drafts = map_view(
        _evt(
            {
                "view": "recovery",
                "prev_view": "autonomy",
                "long_dwell": False,
                "repeat_switch": True,
                "switch_count_today": 4,
                "paired_with_failure": True,
                "failure_summary": "3 schedule runs failed",
            }
        )
    )
    assert len(drafts) == 1
    d = drafts[0]
    assert d.risk_class is RiskClass.PROPOSE
    assert "address ongoing issue" in d.goal.lower()
    assert "recovery" in d.goal
    assert "4" in d.goal


def test_both_thresholds_emit_two_drafts_distinct_event_ids() -> None:
    """Long-dwell + repeat_switch+failure still emit both — independent
    triggers compose. Suffix shape updated to the new key
    (``repeat_switch_failure`` instead of bare ``repeat_switch``)."""
    drafts = map_view(
        _evt(
            {
                "view": "brief",
                "prev_view": "autonomy",
                "long_dwell": True,
                "repeat_switch": True,
                "dwell_seconds": 720.0,
                "switch_count_today": 5,
                "paired_with_failure": True,
            }
        )
    )
    assert len(drafts) == 2
    suffixes = {d.source_event_id.split(":")[-1] for d in drafts}
    assert suffixes == {"long_dwell", "repeat_switch_failure"}
