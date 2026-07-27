from __future__ import annotations

import pytest

from tesseract.orchestrator.activity.hooks import (
    register_routine, remove_routine, fail_routine,
    register_autonomy, remove_autonomy, fail_autonomy,
)
from tesseract.orchestrator.activity.models import ActivityRecord, ActivityRecordOut
from tesseract.orchestrator.activity.registry import get_activity_registry, reset_activity_registry


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    yield
    reset_activity_registry()


def test_register_routine_then_remove():
    register_routine("abc123", label="daily_brief")
    rec = get_activity_registry().get("routine:abc123")
    assert rec is not None
    assert rec.kind == "routine"
    assert rec.state == "running"
    assert rec.durability == "ephemeral"
    assert rec.label == "daily_brief"
    remove_routine("abc123")
    assert get_activity_registry().get("routine:abc123") is None


def test_register_autonomy_then_remove():
    register_autonomy("item-9", label="summarise pulse")
    rec = get_activity_registry().get("autonomy:item-9")
    assert rec is not None
    assert rec.kind == "autonomy"
    assert rec.state == "running"
    remove_autonomy("item-9")
    assert get_activity_registry().get("autonomy:item-9") is None


def test_autonomy_kind_round_trips_wire_model():
    rec = ActivityRecord(
        activity_id="autonomy:x", kind="autonomy", label="x",
        state="running", durability="ephemeral",
    )
    out = ActivityRecordOut.from_record(rec)
    assert out.kind == "autonomy"
    assert out.model_dump()["kind"] == "autonomy"


def test_remove_unknown_id_is_noop():
    remove_routine("never")
    remove_autonomy("never")


def test_fail_routine_transitions_to_failed_and_stays_registered():
    register_routine("abc123", label="daily_brief")
    fail_routine("abc123", detail="ollama unreachable")
    rec = get_activity_registry().get("routine:abc123")
    assert rec is not None, "a failed routine must stay in the registry, not be removed"
    assert rec.state == "failed"
    assert rec.result == "ollama unreachable"


def test_fail_autonomy_transitions_to_failed_and_stays_registered():
    register_autonomy("item-9", label="summarise pulse")
    fail_autonomy("item-9", detail="RunnerException")
    rec = get_activity_registry().get("autonomy:item-9")
    assert rec is not None, "a failed autonomy item must stay in the registry, not be removed"
    assert rec.state == "failed"
    assert rec.result == "RunnerException"


def test_fail_unknown_id_is_noop():
    fail_routine("never", detail="x")
    fail_autonomy("never", detail="x")
