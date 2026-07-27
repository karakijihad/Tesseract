"""AU-20 §10 retrofit — conscience + librarian heartbeats now publish
``self_reflection`` events on meaningful state changes.

Covers:
- conscience_heartbeat fires a bus event on escalation (ok→bad) with
  OPERATOR_GATE risk and the changed signal names as evidence.
- conscience_heartbeat fires PROPOSE on recovery (bad→ok).
- conscience_heartbeat does NOT publish when there's no transition.
- librarian_heartbeat fires a bus event when distillation produces
  ≥1 personality candidate.
- librarian_heartbeat does NOT publish when distillation produces zero
  candidates.
"""

from __future__ import annotations

import pytest

from tesseract.orchestrator.autonomy import publishers
from tesseract.orchestrator.autonomy.event_bus import AutonomyEventBus
from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.scheduler.tasks.conscience_heartbeat import (
    _publish_drift_transition,
)
from tesseract.scheduler.tasks.librarian_heartbeat import (
    _publish_distillation_signal,
)
from tesseract.scheduler.types import JobContext


@pytest.fixture(autouse=True)
def _bus():
    bus = AutonomyEventBus()
    publishers.set_active_bus(bus)
    yield bus
    publishers.set_active_bus(None)


def _ctx() -> JobContext:
    return JobContext(job_name="conscience_heartbeat", run_id="run1234567890abcdef")


def test_conscience_escalation_publishes_operator_gate(_bus: AutonomyEventBus) -> None:
    transition = {
        "from": "ok",
        "to": "bad",
        "changed_signals": [
            {"name": "scheduler_idle", "from": "ok", "to": "bad"},
            {"name": "open_breakers", "from": "ok", "to": "warn"},
        ],
        "memory_id": "mem_abc",
        "flapping": False,
    }
    _publish_drift_transition(transition, _ctx())
    buffered = _bus.peek(AgendaSource.SELF_REFLECTION)
    assert len(buffered) == 1
    ev = buffered[0]
    assert ev.payload["suggested_risk_class"] == "operator_gate"
    assert "ok->bad" in ev.payload["observation"]
    assert "scheduler_idle" in ev.payload["evidence_ids"]
    assert "open_breakers" in ev.payload["evidence_ids"]
    assert "mem_abc" in ev.payload["evidence_ids"]
    assert ev.event_id.startswith("evt_conscience_ok_bad_")


def test_conscience_recovery_publishes_propose(_bus: AutonomyEventBus) -> None:
    transition = {
        "from": "bad",
        "to": "ok",
        "changed_signals": [{"name": "scheduler_idle", "from": "bad", "to": "ok"}],
        "memory_id": None,
        "flapping": False,
    }
    _publish_drift_transition(transition, _ctx())
    buffered = _bus.peek(AgendaSource.SELF_REFLECTION)
    assert len(buffered) == 1
    assert buffered[0].payload["suggested_risk_class"] == "propose"
    assert buffered[0].payload["memory_id"] is None


def test_librarian_publishes_when_candidates_positive(_bus: AutonomyEventBus) -> None:
    _publish_distillation_signal(2, _ctx())
    buffered = _bus.peek(AgendaSource.SELF_REFLECTION)
    assert len(buffered) == 1
    ev = buffered[0]
    assert ev.payload["suggested_risk_class"] == "propose"
    assert "pending personality candidate" in ev.payload["observation"]
    assert ev.event_id.startswith("evt_librarian_")


def test_publish_no_op_when_no_bus_attached() -> None:
    """publish_to_bus drops silently when the bus is None."""
    publishers.set_active_bus(None)
    transition = {"from": "ok", "to": "warn", "changed_signals": [], "memory_id": None}
    # Must not raise.
    _publish_drift_transition(transition, _ctx())
    _publish_distillation_signal(5, _ctx())
