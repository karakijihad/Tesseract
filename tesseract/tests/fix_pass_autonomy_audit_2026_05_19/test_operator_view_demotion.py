"""Codex audit-2 2026-05-19 P1 #2 (audit-1 P1 #4) — operator-view
repeat-switch is demoted from per-switch agenda items to a paired
signal that only fires alongside a real operational failure.

Live system on 2026-05-19 had `view-switch-chat` agenda items at
switch counts 3 / 4 / 5 / 6 / 7 / 8 / 9 — pure UI navigation noise
converted into work. The mapper now drops bare repeat_switch and only
emits when ``paired_with_failure=True`` is stamped on the payload.
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.mappers.operator_view import map as map_operator_view
from tesseract.orchestrator.autonomy.models import AgendaSource


def _event(payload: dict) -> AutonomyEvent:
    return AutonomyEvent.make(AgendaSource.OPERATOR_VIEW, payload)


def test_bare_repeat_switch_no_longer_emits_agenda_draft() -> None:
    """The audit-flagged noise pattern: operator flips tabs 3+ times,
    used to spam the agenda with "propose as default route" items."""
    event = _event({
        "view": "chat",
        "repeat_switch": True,
        "switch_count_today": 9,
    })
    assert map_operator_view(event) == []


def test_long_dwell_still_emits() -> None:
    """Long_dwell is the audit-approved kept signal — operator sat on
    a view for ≥5 min, summarise what they were looking at."""
    event = _event({
        "view": "chat",
        "prev_view": "recovery",
        "long_dwell": True,
        "dwell_seconds": 480.0,
    })
    drafts = map_operator_view(event)
    assert len(drafts) == 1
    assert "summarise" in drafts[0].goal.lower()
    assert "recovery" in drafts[0].goal


def test_repeat_switch_with_failure_pairing_emits() -> None:
    """When the WS handler stamps ``paired_with_failure=True`` (e.g.
    operator keeps checking the recovery view because it's still red),
    the mapper proposes a fix item."""
    event = _event({
        "view": "recovery",
        "repeat_switch": True,
        "switch_count_today": 4,
        "paired_with_failure": True,
        "failure_summary": "3 schedule runs failed in the last hour",
    })
    drafts = map_operator_view(event)
    assert len(drafts) == 1
    assert "address ongoing issue" in drafts[0].goal.lower()
    assert "recovery" in drafts[0].goal
    assert "3 schedule runs" in drafts[0].rationale


def test_repeat_switch_without_failure_drops() -> None:
    """Without paired_with_failure, repeat_switch is still observable
    through /api/operator/presence but does not enter the agenda."""
    event = _event({
        "view": "brief",
        "repeat_switch": True,
        "switch_count_today": 7,
        "paired_with_failure": False,
    })
    assert map_operator_view(event) == []


def test_repeat_switch_requires_literal_failure_pairing_true() -> None:
    """False-like strings must not become actionable via bool(str)."""
    event = _event({
        "view": "brief",
        "repeat_switch": True,
        "switch_count_today": 7,
        "paired_with_failure": "false",
    })
    assert map_operator_view(event) == []


def test_long_dwell_plus_repeat_switch_failure_emits_both() -> None:
    """Independent triggers fire independently — composition is OR."""
    event = _event({
        "view": "recovery",
        "prev_view": "agenda",
        "long_dwell": True,
        "dwell_seconds": 600.0,
        "repeat_switch": True,
        "switch_count_today": 5,
        "paired_with_failure": True,
        "failure_summary": "active blocked items",
    })
    drafts = map_operator_view(event)
    assert len(drafts) == 2
