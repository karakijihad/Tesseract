"""AU-4 — AgendaItem model + transitions + dedupe + id minting."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    dedupe_key,
    mint_agenda_id,
)
from tesseract.tests.fix_pass_autonomy_AU_4.conftest import make_item


def test_mint_agenda_id_shape() -> None:
    when = datetime(2026, 5, 18, 14, 32, tzinfo=timezone.utc)
    iid = mint_agenda_id("Research OpenClaw", now=when)
    assert iid.startswith("ag-2026-05-18-1432-")
    assert iid.endswith("research-openclaw")


def test_mint_agenda_id_strips_punctuation() -> None:
    when = datetime(2026, 5, 18, 14, 32, tzinfo=timezone.utc)
    iid = mint_agenda_id("!! hello!! world ??", now=when)
    assert iid == "ag-2026-05-18-1432-hello-world"


def test_mint_agenda_id_handles_empty_slug() -> None:
    when = datetime(2026, 5, 18, 14, 32, tzinfo=timezone.utc)
    iid = mint_agenda_id("???", now=when)
    assert iid == "ag-2026-05-18-1432-item"


def test_item_round_trip_via_model_dump() -> None:
    item = make_item(goal="audit-doe-flow")
    raw = item.model_dump(mode="json")
    rehydrated = AgendaItem.model_validate(raw)
    assert rehydrated.id == item.id
    assert rehydrated.status == AgendaStatus.PROPOSED
    assert rehydrated.risk_class == RiskClass.AUTONOMOUS
    assert rehydrated.source == AgendaSource.SELF_REFLECTION


def test_extra_fields_rejected() -> None:
    item = make_item()
    raw = json.loads(item.model_dump_json())
    raw["typo_field"] = "nope"
    with pytest.raises(ValidationError):
        AgendaItem.model_validate(raw)


def test_transition_to_appends_history() -> None:
    item = make_item()
    item.transition_to(AgendaStatus.SELECTED, reason="kernel_pick", by="kernel")
    assert item.status == AgendaStatus.SELECTED
    assert len(item.status_history) == 1
    entry = item.status_history[0]
    assert entry.from_status == AgendaStatus.PROPOSED
    assert entry.to_status == AgendaStatus.SELECTED
    assert entry.reason == "kernel_pick"
    assert entry.by == "kernel"


def test_transition_noop_when_same_status() -> None:
    item = make_item(status=AgendaStatus.RUNNING)
    item.transition_to(AgendaStatus.RUNNING, reason="ignored")
    assert item.status_history == []


def test_terminal_check() -> None:
    assert not make_item(status=AgendaStatus.RUNNING).is_terminal()
    assert make_item(status=AgendaStatus.DONE).is_terminal()
    assert make_item(status=AgendaStatus.CANCELLED).is_terminal()
    assert make_item(status=AgendaStatus.ABANDONED).is_terminal()


def test_operator_priority_bounds() -> None:
    with pytest.raises(ValidationError):
        make_item(operator_priority=99)
    with pytest.raises(ValidationError):
        make_item(operator_priority=-10)


def test_goal_max_length() -> None:
    with pytest.raises(ValidationError):
        make_item(goal="x" * 501)


def test_unvetted_status_round_trips_and_is_not_terminal() -> None:
    item = make_item(status=AgendaStatus.UNVETTED)
    raw = item.model_dump(mode="json")
    rehydrated = AgendaItem.model_validate(raw)
    assert rehydrated.status == AgendaStatus.UNVETTED
    assert not rehydrated.is_terminal()


def test_dedupe_key_normalises_whitespace_and_case() -> None:
    k1 = dedupe_key("Audit  the   repo", AgendaSource.OPERATOR)
    k2 = dedupe_key("audit the repo", AgendaSource.OPERATOR)
    assert k1 == k2


def test_dedupe_key_distinguishes_sources() -> None:
    k1 = dedupe_key("audit the repo", AgendaSource.OPERATOR)
    k2 = dedupe_key("audit the repo", AgendaSource.SELF_REFLECTION)
    assert k1 != k2
