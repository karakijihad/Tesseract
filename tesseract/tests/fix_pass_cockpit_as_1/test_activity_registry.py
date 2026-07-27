"""AS-1 Phase 1 — Unified Activity Registry core (in-memory, no integration).

Pure unit tests: register/update/remove/get/snapshot + the `activity`-channel
event emission via the background bus ring buffer. No disk, no event loop —
the registry is in-memory and the bus exposes a synchronous ring snapshot.
"""

import pytest

from tesseract.orchestrator.activity import (
    ActivityRecord,
    get_activity_registry,
    reset_activity_registry,
)
from tesseract.orchestrator.activity.events import CHANNEL
from tesseract.orchestrator.background_event_bus import get_background_bus


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_activity_registry()
    yield
    reset_activity_registry()


def _rec(activity_id: str, **over) -> ActivityRecord:
    base = dict(
        activity_id=activity_id,
        kind="lane",
        label="coder/claude",
        state="running",
        durability="persistent",
        provider="claude",
    )
    base.update(over)
    return ActivityRecord(**base)


def _activity_events_for(activity_id: str):
    """Events on the `activity` channel for one id, oldest-first, from the bus
    ring buffer (filter by unique id so cross-test accumulation is irrelevant)."""
    out = []
    for ev in get_background_bus().snapshot():
        data = ev.data or {}
        if data.get("channel") == CHANNEL and data.get("session_id") == activity_id:
            out.append((ev.type, data["data"]["state"]))
    return out


def test_register_adds_record_and_stamps_times():
    reg = get_activity_registry()
    reg.register(_rec("lane:L1", started_at=""))
    got = reg.get("lane:L1")
    assert got is not None
    assert got.label == "coder/claude"
    assert got.started_at and got.updated_at  # stamped on register


def test_register_emits_registered_then_updated():
    reg = get_activity_registry()
    reg.register(_rec("lane:L2"))
    reg.register(_rec("lane:L2", state="idle"))  # re-register = update
    events = _activity_events_for("lane:L2")
    assert events[0][0] == "activity_registered"
    assert events[-1][0] == "activity_updated"
    assert events[-1][1] == "idle"


def test_register_preserves_started_at_on_reregister():
    reg = get_activity_registry()
    reg.register(_rec("lane:L3"))
    first = reg.get("lane:L3").started_at
    reg.register(_rec("lane:L3", state="idle"))
    assert reg.get("lane:L3").started_at == first  # original start preserved


def test_update_state_transitions_and_emits():
    reg = get_activity_registry()
    reg.register(_rec("lane:L4", state="running"))
    reg.update_state("lane:L4", "closed")
    assert reg.get("lane:L4").state == "closed"
    assert _activity_events_for("lane:L4")[-1] == ("activity_updated", "closed")


def test_update_state_unknown_id_is_noop():
    reg = get_activity_registry()
    reg.update_state("lane:nope", "closed")  # must not raise
    assert reg.get("lane:nope") is None
    assert _activity_events_for("lane:nope") == []


def test_remove_drops_record_and_emits_removed():
    reg = get_activity_registry()
    reg.register(_rec("delegate:D1", kind="delegate", durability="ephemeral"))
    reg.remove("delegate:D1")
    assert reg.get("delegate:D1") is None
    assert _activity_events_for("delegate:D1")[-1][0] == "activity_removed"


def test_remove_unknown_id_is_noop():
    reg = get_activity_registry()
    reg.remove("delegate:ghost")  # must not raise or emit
    assert _activity_events_for("delegate:ghost") == []


def test_snapshot_returns_wire_models():
    reg = get_activity_registry()
    reg.register(_rec("lane:L5"))
    reg.register(_rec("session:S1", kind="controller_session", provider=None, label="mission"))
    snap = reg.snapshot()
    ids = {r.activity_id for r in snap}
    assert ids == {"lane:L5", "session:S1"}
    # ActivityRecordOut is the wire projection (Pydantic), serializable.
    assert all(hasattr(r, "model_dump") for r in snap)


def test_envelope_shape():
    reg = get_activity_registry()
    reg.register(_rec("lane:L6"))
    ev = next(
        e for e in get_background_bus().snapshot()
        if (e.data or {}).get("session_id") == "lane:L6"
    )
    assert ev.data["channel"] == CHANNEL
    assert ev.data["kind"] == "activity_registered"
    assert ev.data["ts"]
    assert ev.data["data"]["activity_id"] == "lane:L6"
    assert ev.data["data"]["durability"] == "persistent"


def test_singleton_identity_and_reset():
    a = get_activity_registry()
    assert a is get_activity_registry()
    reset_activity_registry()
    assert get_activity_registry() is not a
