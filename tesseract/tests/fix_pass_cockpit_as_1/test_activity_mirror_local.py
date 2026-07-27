"""AS-1 Batch 1 (Mirror-local) — delegate hooks (Phase 3), REST snapshot +
WS pump (Phase 5), disk rebuild (Phase 6).

No controller daemon, no IPC: everything here lives in the Mirror process.
Delegates register in-process; lanes + controller sessions are re-indexed
from their canonical on-disk files. Each test resets the process-global
registry; disk-touching tests pin TESSERACT_HOME to tmp_path so nothing
writes under the real ``tesseract/logs`` tree.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tesseract.kernel.tools.base import ToolResult
from tesseract.orchestrator.activity import (
    ActivityRecord,
    get_activity_registry,
    reset_activity_registry,
)
from tesseract.orchestrator.activity.rebuild import rebuild_from_disk


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_activity_registry()
    yield
    reset_activity_registry()


# ── Phase 3 — delegate hooks (SpawnRegistry) ───────────────────────────────


async def test_spawn_register_projects_running_delegate():
    from tesseract.brain.spawns import SpawnRegistry

    reg = get_activity_registry()
    sr = SpawnRegistry()
    gate = asyncio.Event()

    async def _work() -> ToolResult:
        await gate.wait()
        return ToolResult(output="ok")

    handle = sr.register(kind="delegate_claude", coro=_work())
    rec = reg.get(f"delegate:{handle.handle_id}")
    assert rec is not None
    assert rec.kind == "delegate"
    assert rec.state == "running"
    assert rec.durability == "ephemeral"
    assert rec.provider == "claude"

    gate.set()
    await handle.task
    await asyncio.sleep(0)  # let the done-callback run
    assert reg.get(f"delegate:{handle.handle_id}").state == "done"


async def test_spawn_failure_marks_failed_with_codex_provider():
    from tesseract.brain.spawns import SpawnRegistry

    reg = get_activity_registry()
    sr = SpawnRegistry()

    async def _boom() -> ToolResult:
        raise RuntimeError("intentional")

    handle = sr.register(kind="delegate_codex", coro=_boom())
    with pytest.raises(RuntimeError):
        await handle.task
    await asyncio.sleep(0)
    rec = reg.get(f"delegate:{handle.handle_id}")
    assert rec.state == "failed"
    assert rec.provider == "codex"


async def test_spawn_cancel_marks_cancelled_agent_provider_none():
    from tesseract.brain.spawns import SpawnRegistry

    reg = get_activity_registry()
    sr = SpawnRegistry()

    async def _forever() -> ToolResult:
        await asyncio.Event().wait()
        return ToolResult(output="never")

    handle = sr.register(kind="agent:researcher", coro=_forever())
    assert reg.get(f"delegate:{handle.handle_id}").provider is None  # not claude/codex
    await sr.cancel(handle.handle_id)
    await asyncio.sleep(0)
    assert reg.get(f"delegate:{handle.handle_id}").state == "cancelled"


# ── Phase 5 — REST snapshot + WS pump ──────────────────────────────────────


async def test_rest_snapshot_serializes_items():
    from tesseract.mirror.server.routes import activity as activity_route

    reg = get_activity_registry()
    reg.register(
        ActivityRecord(
            activity_id="delegate:d1",
            kind="delegate",
            label="delegate_claude",
            state="running",
            durability="ephemeral",
            provider="claude",
        )
    )
    resp = await activity_route.list_activity(None)  # handler ignores request
    payload = json.loads(resp.body)
    assert [i["activity_id"] for i in payload["items"]] == ["delegate:d1"]
    assert payload["items"][0]["state"] == "running"


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_json(self, env: dict) -> None:
        self.sent.append(env)


class _FakeSession:
    def __init__(self) -> None:
        self.session_id = "test-session"
        self.ws = _FakeWS()
        self.event_log = []  # send_envelope appends here


async def test_ws_pump_forwards_only_activity_channel():
    from tesseract.mirror.server import ws as ws_mod

    session = _FakeSession()
    pump = asyncio.create_task(ws_mod._activity_events_pump(None, session))
    await asyncio.sleep(0)  # let the pump subscribe before we publish

    reg = get_activity_registry()
    reg.register(
        ActivityRecord(
            activity_id="lane:L1",
            kind="lane",
            label="coder/claude",
            state="running",
            durability="persistent",
            provider="claude",
        )
    )
    # An unrelated channel event must NOT be forwarded.
    from tesseract.orchestrator.background_event_bus import get_background_bus

    get_background_bus().publish(
        "noise", {"channel": "surface", "session_id": "x", "data": {}}
    )

    for _ in range(50):
        await asyncio.sleep(0)
        if session.ws.sent:
            break

    session.ws.closed = True
    pump.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass

    channels = {e.get("channel") for e in session.ws.sent}
    assert channels == {"activity"}
    assert session.ws.sent[0]["data"]["activity_id"] == "lane:L1"


# ── Phase 6 — rebuild_from_disk ────────────────────────────────────────────


def _seed_lane(tmp_path, lane_id: str, *, lifecycle: str = "ready", kind: str = "claude"):
    from tesseract.orchestrator.tars_controller.lanes.models import Lane
    from tesseract.orchestrator.tars_controller.lanes.store import write_lane

    write_lane(
        Lane(
            lane_id=lane_id,
            kind=kind,
            mode="headless",
            model="m",
            working_dir=str(tmp_path),
            lifecycle=lifecycle,
        )
    )


def test_rebuild_named_lane_uses_name_label_and_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.lanes.named import (
        NamedLaneRecord,
        write_named_lane,
    )

    _seed_lane(tmp_path, "lane-claude-aaa", lifecycle="busy")
    write_named_lane(
        NamedLaneRecord(
            name="coder/claude",
            lane_id="lane-claude-aaa",
            kind="claude",
            model="m",
            working_dir=str(tmp_path),
        )
    )

    n = rebuild_from_disk()
    assert n == 1
    rec = get_activity_registry().get("lane:lane-claude-aaa")
    assert rec is not None
    assert rec.kind == "lane"
    assert rec.label == "coder/claude"  # name, not the bare id
    assert rec.state == "running"  # busy → running
    assert rec.provider == "claude"
    assert rec.durability == "persistent"


def test_rebuild_skips_closed_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    _seed_lane(tmp_path, "lane-claude-dead", lifecycle="closed")
    n = rebuild_from_disk()
    assert n == 0
    assert get_activity_registry().get("lane:lane-claude-dead") is None


def test_rebuild_bare_lane_keeps_id_label(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    _seed_lane(tmp_path, "lane-codex-bbb", lifecycle="ready", kind="codex")
    n = rebuild_from_disk()
    assert n == 1
    rec = get_activity_registry().get("lane:lane-codex-bbb")
    assert rec.label == "lane-codex-bbb"
    assert rec.provider == "codex"
    assert rec.state == "idle"  # ready → idle


def test_rebuild_controller_session_status_mapped(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.sessions import SessionRegistry

    rec = SessionRegistry().create_session(
        mode="chat", origin="mirror", title="Working session"
    )
    SessionRegistry().update_session(rec.session_id, status="active")

    # create/update_session self-register via the Phase-4 hooks; reset so this
    # test exercises rebuild reading DISK, not the live hook's residue.
    reset_activity_registry()
    n = rebuild_from_disk()
    assert n == 1
    got = get_activity_registry().get(f"session:{rec.session_id}")
    assert got is not None
    assert got.kind == "controller_session"
    assert got.label == "Working session"
    assert got.state == "running"  # active → running
    assert got.durability == "persistent"


def test_rebuild_skips_closing_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    _seed_lane(tmp_path, "lane-claude-closing", lifecycle="closing")
    n = rebuild_from_disk()
    assert n == 0
    assert get_activity_registry().get("lane:lane-claude-closing") is None


def test_rebuild_does_not_publish_to_bus(tmp_path, monkeypatch):
    # Boot seed runs off-loop via asyncio.to_thread; it must NOT touch the
    # loop-thread-only bus. REST hydration is the catch-up path.
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.activity.events import CHANNEL
    from tesseract.orchestrator.background_event_bus import get_background_bus

    _seed_lane(tmp_path, "lane-claude-quiet", lifecycle="ready")
    rebuild_from_disk()

    assert get_activity_registry().get("lane:lane-claude-quiet") is not None
    activity_events = [
        ev
        for ev in get_background_bus().snapshot()
        if (ev.data or {}).get("channel") == CHANNEL
        and (ev.data or {}).get("session_id") == "lane:lane-claude-quiet"
    ]
    assert activity_events == []


def test_rebuild_skips_closed_session(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    from tesseract.orchestrator.tars_controller.sessions import SessionRegistry

    rec = SessionRegistry().create_session(mode="chat", origin="mirror")
    SessionRegistry().update_session(rec.session_id, status="closed")
    # Isolate rebuild from the create/update hooks (see above).
    reset_activity_registry()
    n = rebuild_from_disk()
    assert n == 0
    assert get_activity_registry().get(f"session:{rec.session_id}") is None
