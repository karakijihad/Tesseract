"""AS-1 Batch 2 (cross-process live push) — controller-side hooks (Phase 2/4),
the protocol push, the daemon activity forwarder, and the Mirror subscriber.

Process-boundary note: the controller hooks publish on the controller's bus
and the daemon forwarder broadcasts an ``ActivityEventPush``; the Mirror
subscriber re-applies it to the Mirror registry. These tests exercise both
halves and the wire round-trip without standing up a real TCP daemon. Disk
writers pin TESSERACT_HOME to tmp_path so nothing touches ``tesseract/logs``.
"""

from __future__ import annotations

import asyncio

import pytest

from tesseract.orchestrator.activity import (
    get_activity_registry,
    reset_activity_registry,
)
from tesseract.orchestrator.activity.events import CHANNEL
from tesseract.orchestrator.activity.hooks import register_lane
from tesseract.orchestrator.background_event_bus import get_background_bus


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_activity_registry()
    yield
    reset_activity_registry()


class _StubAdapter:
    def __init__(self, lane_id: str, *, on_run=None) -> None:
        self._lane_id = lane_id
        self._on_run = on_run

    async def run_turn(self, *, message, on_event, cancel_event):
        if self._on_run is not None:
            self._on_run()
        return {"session_id": "s", "is_error": False, "usage": {}}


# ── Phase 2 — lane hooks (LaneManager) ─────────────────────────────────────


async def test_lane_open_registers_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.lanes.manager import LaneManager

    lm = LaneManager(adapter_factory=lambda lane, rt: _StubAdapter(lane.lane_id))
    lane_id = await lm.open(
        kind="claude", mode="headless", model="m", working_dir=str(tmp_path)
    )
    rec = get_activity_registry().get(f"lane:{lane_id}")
    assert rec is not None
    assert rec.kind == "lane"
    assert rec.state == "idle"  # ready → idle
    assert rec.provider == "claude"
    assert rec.durability == "persistent"
    assert rec.label == lane_id  # bare; ensure upserts the name


async def test_lane_turn_flips_running_then_idle(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.lanes.manager import LaneManager

    seen: dict[str, str] = {}

    def _factory(lane, rt):
        def _capture_mid_turn():
            rec = get_activity_registry().get(f"lane:{lane.lane_id}")
            seen["mid"] = rec.state if rec else "<missing>"

        return _StubAdapter(lane.lane_id, on_run=_capture_mid_turn)

    lm = LaneManager(adapter_factory=_factory)
    lane_id = await lm.open(
        kind="claude", mode="headless", model="m", working_dir=str(tmp_path)
    )
    await lm.send(lane_id, "hello")  # fire-and-queue: ack, turn on a task
    await lm.drain(lane_id)          # settle the turn before asserting
    assert seen["mid"] == "running"  # busy during the turn
    assert get_activity_registry().get(f"lane:{lane_id}").state == "idle"  # after


async def test_lane_close_marks_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.lanes.manager import LaneManager

    lm = LaneManager(adapter_factory=lambda lane, rt: _StubAdapter(lane.lane_id))
    lane_id = await lm.open(
        kind="codex", mode="headless", model="m", working_dir=str(tmp_path)
    )
    await lm.close(lane_id, "operator_close")
    assert get_activity_registry().get(f"lane:{lane_id}").state == "closed"


async def test_named_ensure_registers_with_name_label(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.lanes.manager import LaneManager
    from tesseract.orchestrator.tars_controller.lanes.named import NamedLaneManager

    lm = LaneManager(adapter_factory=lambda lane, rt: _StubAdapter(lane.lane_id))
    nlm = NamedLaneManager(lane_manager=lm)
    record = await nlm.ensure(
        "coder/claude", kind="claude", model="m", working_dir=str(tmp_path)
    )
    rec = get_activity_registry().get(f"lane:{record.lane_id}")
    assert rec is not None
    assert rec.label == "coder/claude"  # name, not the bare id
    assert rec.provider == "claude"


# ── Phase 4 — controller session hooks (SessionRegistry) ───────────────────


def test_session_create_registers_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.sessions import SessionRegistry

    rec = SessionRegistry().create_session(
        mode="chat", origin="mirror", title="Working session"
    )
    a = get_activity_registry().get(f"session:{rec.session_id}")
    assert a is not None
    assert a.kind == "controller_session"
    assert a.label == "Working session"
    assert a.state == "running"  # active → running
    assert a.durability == "persistent"


def test_session_update_transitions_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.sessions import SessionRegistry

    reg = SessionRegistry()
    rec = reg.create_session(mode="chat", origin="mirror")
    reg.update_session(rec.session_id, status="idle")
    assert get_activity_registry().get(f"session:{rec.session_id}").state == "idle"
    reg.update_session(rec.session_id, status="closed")
    assert get_activity_registry().get(f"session:{rec.session_id}").state == "closed"


# ── protocol + daemon forwarder + Mirror subscriber ────────────────────────


def test_activity_event_push_shape():
    from tesseract.orchestrator.tars_controller.protocol import ActivityEventPush

    push = ActivityEventPush(
        envelope={"kind": "activity_registered", "channel": CHANNEL, "data": {}}
    ).model_dump(mode="json")
    assert push["push"] is True
    assert push["event"] == "activity_event"
    assert push["envelope"]["kind"] == "activity_registered"


async def test_daemon_forwarder_broadcasts_activity_events():
    from tesseract.orchestrator.tars_controller.daemon import ControllerDaemon

    daemon = ControllerDaemon(controller_id="t", token="tok")
    captured: list[dict] = []

    async def _capture(push, *, exclude_writer_id=None):
        captured.append(push)

    daemon._broadcast_to_all = _capture  # type: ignore[assignment]
    task = asyncio.create_task(daemon._activity_forward_loop())
    await asyncio.sleep(0)  # let the loop subscribe before we publish

    register_lane("FWD", label="x", provider="claude", lifecycle="ready")
    for _ in range(50):
        await asyncio.sleep(0)
        if captured:
            break

    daemon._stop_event.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert captured, "forwarder did not broadcast the activity event"
    assert captured[0]["event"] == "activity_event"
    assert captured[0]["envelope"]["session_id"] == "lane:FWD"
    assert captured[0]["envelope"]["channel"] == CHANNEL


def test_subscriber_apply_register_update_remove():
    from tesseract.mirror.server.activity_subscriber import ActivitySubscriber

    sub = ActivitySubscriber()
    data = {
        "activity_id": "lane:X",
        "kind": "lane",
        "label": "coder/claude",
        "state": "running",
        "durability": "persistent",
        "provider": "claude",
        "parent_turn_id": None,
        "parent_session_id": None,
        "transcript_ref": "controller/lanes/X/transcript.txt",
        "started_at": "t0",
        "updated_at": "t0",
    }
    sub._apply({"kind": "activity_registered", "data": data})
    assert get_activity_registry().get("lane:X").state == "running"
    sub._apply({"kind": "activity_updated", "data": {**data, "state": "idle"}})
    assert get_activity_registry().get("lane:X").state == "idle"
    sub._apply({"kind": "activity_removed", "data": data})
    assert get_activity_registry().get("lane:X") is None


def test_subscriber_apply_bad_envelope_does_not_crash():
    from tesseract.mirror.server.activity_subscriber import ActivitySubscriber

    sub = ActivitySubscriber()
    sub._apply({"kind": "activity_registered", "data": {}})  # missing fields
    sub._apply({})  # no kind/data
    assert get_activity_registry().snapshot() == []  # nothing registered


def test_controller_hook_to_mirror_round_trip(tmp_path, monkeypatch):
    # Full wire contract without a socket: a controller hook publishes on the
    # bus → the forwarder would wrap it as ActivityEventPush → the Mirror
    # subscriber re-applies the envelope to a (separate) Mirror registry.
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.mirror.server.activity_subscriber import ActivitySubscriber
    from tesseract.orchestrator.tars_controller.protocol import ActivityEventPush

    register_lane("E2E", label="coder/claude", provider="claude", lifecycle="busy")
    envelope = next(
        e.data
        for e in get_background_bus().snapshot()
        if (e.data or {}).get("channel") == CHANNEL
        and (e.data or {}).get("session_id") == "lane:E2E"
    )
    push = ActivityEventPush(envelope=envelope).model_dump(mode="json")

    reset_activity_registry()  # mimic the separate Mirror-process registry
    ActivitySubscriber()._apply(push["envelope"])
    rec = get_activity_registry().get("lane:E2E")
    assert rec is not None
    assert rec.state == "running"  # busy → running survived the round-trip
    assert rec.label == "coder/claude"
    assert rec.provider == "claude"


# ── P1 gap-a — snapshot-on-(re)connect ─────────────────────────────────────


def _out_dict(activity_id: str, *, state: str = "running") -> dict:
    return {
        "activity_id": activity_id,
        "kind": "lane",
        "label": activity_id,
        "state": state,
        "durability": "persistent",
        "provider": "claude",
        "parent_turn_id": None,
        "parent_session_id": None,
        "transcript_ref": None,
        "started_at": "t0",
        "updated_at": "t0",
    }


def test_activity_snapshot_message_parses():
    from tesseract.orchestrator.tars_controller.protocol import (
        ActivitySnapshotMessage,
        parse_client_message,
    )

    parsed = parse_client_message({"msg": "activity_snapshot"})
    assert isinstance(parsed, ActivitySnapshotMessage)


def test_activity_snapshot_push_shape():
    from tesseract.orchestrator.tars_controller.protocol import ActivitySnapshotPush

    push = ActivitySnapshotPush(records=[_out_dict("lane:Z")]).model_dump(mode="json")
    assert push["push"] is True
    assert push["event"] == "activity_snapshot"
    assert push["records"][0]["activity_id"] == "lane:Z"


async def test_daemon_on_activity_snapshot_replies_with_records():
    import types

    from tesseract.orchestrator.tars_controller.daemon import ControllerDaemon
    from tesseract.orchestrator.tars_controller.protocol import (
        ActivitySnapshotMessage,
    )

    register_lane("SNAP", label="coder/claude", provider="claude", lifecycle="busy")
    daemon = ControllerDaemon(controller_id="t", token="tok")
    conn = types.SimpleNamespace(outbound=asyncio.Queue())

    await daemon._on_activity_snapshot(conn, ActivitySnapshotMessage())

    push = conn.outbound.get_nowait()
    assert push["event"] == "activity_snapshot"
    ids = {r["activity_id"] for r in push["records"]}
    assert "lane:SNAP" in ids


def test_subscriber_apply_snapshot_upserts_all():
    from tesseract.mirror.server.activity_subscriber import ActivitySubscriber

    sub = ActivitySubscriber()
    sub._apply_snapshot([_out_dict("lane:A"), _out_dict("lane:B"), _out_dict("lane:C")])
    ids = {r.activity_id for r in get_activity_registry().snapshot()}
    assert {"lane:A", "lane:B", "lane:C"} <= ids


def test_subscriber_apply_snapshot_drops_bad_record_keeps_rest():
    from tesseract.mirror.server.activity_subscriber import ActivitySubscriber

    sub = ActivitySubscriber()
    sub._apply_snapshot([_out_dict("lane:OK"), {"bogus": 1}])  # 2nd is malformed
    ids = {r.activity_id for r in get_activity_registry().snapshot()}
    assert "lane:OK" in ids  # good one survived; bad one dropped, no crash


async def test_request_snapshot_sends_frame():
    from tesseract.kernel.sandbox._ipc_frames import encode_frame
    from tesseract.orchestrator.tars_controller.ipc_client import ControllerClient

    class _FakeWriter:
        def __init__(self) -> None:
            self.frames: list[bytes] = []

        def write(self, b: bytes) -> None:
            self.frames.append(b)

        async def drain(self) -> None:
            pass

    writer = _FakeWriter()
    client = ControllerClient(reader=None, writer=writer, token="t")  # type: ignore[arg-type]
    await client.request_snapshot()
    assert writer.frames == [encode_frame({"msg": "activity_snapshot"})]


async def test_subscriber_reconciles_full_set_on_connect():
    """Exit criterion: connect to a controller pre-seeded with 3 records → all 3
    appear in the Mirror registry after connect, with no activity_event deltas."""
    from tesseract.mirror.server.activity_subscriber import ActivitySubscriber
    from tesseract.orchestrator.tars_controller.ipc_client import (
        ControllerClientError,
    )

    records = [_out_dict("lane:A"), _out_dict("lane:B"), _out_dict("lane:C")]

    class _FakeClient:
        def __init__(self) -> None:
            self.snapshot_requested = False

        async def request_snapshot(self) -> None:
            self.snapshot_requested = True

        async def pushes(self):
            yield {"event": "activity_snapshot", "records": records}
            yield {"event": "_disconnected"}

        async def close(self) -> None:
            pass

    fake = _FakeClient()
    sub = ActivitySubscriber(backoff_initial=0.01, backoff_max=0.01)
    calls = {"n": 0}

    async def _connect():
        calls["n"] += 1
        if calls["n"] == 1:
            return fake
        sub._stop.set()  # second reconnect → unwind the run loop
        raise ControllerClientError("stop")

    sub._connect = _connect  # type: ignore[assignment]
    await sub.start()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(get_activity_registry().snapshot()) >= 3:
            break
    await sub.stop()

    assert fake.snapshot_requested
    ids = {r.activity_id for r in get_activity_registry().snapshot()}
    assert {"lane:A", "lane:B", "lane:C"} <= ids


# ── P1 gap-b — controller boot re-index of named-lane labels ───────────────


def test_update_lane_state_is_noop_without_record(tmp_path, monkeypatch):
    """The bug gap-b fixes: a state transition on a lane the registry doesn't
    know is silently dropped (update_state no-ops on unknown id)."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.activity.hooks import update_lane_state

    update_lane_state("ghost", "busy")  # nothing seeded for "ghost"
    assert get_activity_registry().get("lane:ghost") is None


async def test_controller_seed_reindexes_named_lane_with_human_label(
    tmp_path, monkeypatch
):
    """gap-b — after a controller restart (empty registry) the boot seed must
    re-register a named lane under its HUMAN label (not the bare lane_id), and a
    subsequent live transition must land AND preserve that label."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.activity.hooks import update_lane_state
    from tesseract.orchestrator.tars_controller.daemon import ControllerDaemon
    from tesseract.orchestrator.tars_controller.lanes.manager import LaneManager
    from tesseract.orchestrator.tars_controller.lanes.named import NamedLaneManager

    lm = LaneManager(adapter_factory=lambda lane, rt: _StubAdapter(lane.lane_id))
    nlm = NamedLaneManager(lane_manager=lm)
    record = await nlm.ensure(
        "coder/claude", kind="claude", model="m", working_dir=str(tmp_path)
    )

    # Mimic a controller restart: wipe the in-memory registry (disk records stay).
    reset_activity_registry()
    assert get_activity_registry().get(f"lane:{record.lane_id}") is None

    daemon = ControllerDaemon(controller_id="t", token="tok")
    await daemon._seed_activity_registry()

    rec = get_activity_registry().get(f"lane:{record.lane_id}")
    assert rec is not None
    assert rec.label == "coder/claude"  # human label, not the bare id

    # The dropped-update is now closed: the transition lands and keeps the label.
    update_lane_state(record.lane_id, "busy")
    rec2 = get_activity_registry().get(f"lane:{record.lane_id}")
    assert rec2.state == "running"
    assert rec2.label == "coder/claude"
