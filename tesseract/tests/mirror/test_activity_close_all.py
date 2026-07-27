"""POST /api/activity/close-all and /api/activity/{activity_id}/close —
bulk and per-item cancel over cancellable running units.

Closes lanes (controller IPC), mcp_sessions (MCPServer), delegates (spawn
registry); skips controller_session/mission_step/routine/autonomy.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.controller_ws import APP_FACTORY_KEY
from tesseract.mirror.server.routes import activity_control
from tesseract.orchestrator.activity import get_activity_registry
from tesseract.orchestrator.activity.models import ActivityRecord
from tesseract.orchestrator.activity.registry import reset_activity_registry
from tesseract.orchestrator.background_event_bus import reset_background_bus


class _FakeClient:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def lane_close(self, lane_id: str, reason: str) -> dict:
        self.closed.append(lane_id)
        return {"ok": True}

    async def close(self) -> None:
        pass


class _FakeMCP:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_session(self, activity_id: str) -> bool:
        self.cancelled.append(activity_id)
        return True


class _FakeSpawns:
    def __init__(self, ids) -> None:
        self._ids = set(ids)
        self.cancelled: list[str] = []

    async def cancel(self, handle_id: str) -> bool:
        if handle_id in self._ids:
            self.cancelled.append(handle_id)
            return True
        return False


def _rec(activity_id: str, kind: str, *, durability: str) -> ActivityRecord:
    return ActivityRecord(
        activity_id=activity_id, kind=kind, label=activity_id,
        state="running", durability=durability,
    )


async def _make_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, _FakeClient, _FakeMCP, _FakeSpawns]:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    reset_background_bus()
    reg = get_activity_registry()
    reg.register(_rec("lane:lane-claude-1", "lane", durability="persistent"))
    reg.register(_rec("mcp:operator:abc", "mcp_session", durability="ephemeral"))
    reg.register(_rec("delegate:h1", "delegate", durability="ephemeral"))
    reg.register(_rec("session:keep", "controller_session", durability="persistent"))

    fake_client = _FakeClient()
    fake_mcp = _FakeMCP()
    fake_spawns = _FakeSpawns(["h1"])

    async def _factory():
        return fake_client

    app = web.Application()
    activity_control.register(app)
    app[APP_FACTORY_KEY] = _factory
    app["mcp_server"] = fake_mcp
    app["server_sessions"] = {"s": SimpleNamespace(chat_session=SimpleNamespace(spawns=fake_spawns))}

    client = TestClient(TestServer(app))
    await client.start_server()
    return client, fake_client, fake_mcp, fake_spawns


@pytest.mark.asyncio
async def test_close_all_cancels_cancellable_and_skips_others(tmp_path, monkeypatch) -> None:
    client, fake_client, fake_mcp, fake_spawns = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.post("/api/activity/close-all")
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert set(body["closed"]) == {"lane:lane-claude-1", "mcp:operator:abc", "delegate:h1"}
    assert body["skipped"] == ["session:keep"]
    assert body["errored"] == []
    # each substrate was driven
    assert fake_client.closed == ["lane-claude-1"]
    assert fake_mcp.cancelled == ["mcp:operator:abc"]
    assert fake_spawns.cancelled == ["h1"]
    # the mcp_session chip is dropped; the skipped session stays
    reg = get_activity_registry()
    assert reg.get("mcp:operator:abc") is None
    assert reg.get("session:keep") is not None


@pytest.mark.asyncio
async def test_close_all_controller_offline_reports_lane_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    reset_background_bus()
    get_activity_registry().register(_rec("lane:lane-x", "lane", durability="persistent"))

    from tesseract.orchestrator.tars_controller.ipc_client import ControllerClientError

    async def _factory():
        raise ControllerClientError("daemon down")

    app = web.Application()
    activity_control.register(app)
    app[APP_FACTORY_KEY] = _factory
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/activity/close-all")
        body = await resp.json()
    finally:
        await client.close()
    assert body["errored"] == ["lane:lane-x"]
    assert body["closed"] == []


@pytest.mark.asyncio
async def test_close_one_cancels_a_single_lane(tmp_path, monkeypatch) -> None:
    client, fake_client, _fake_mcp, _fake_spawns = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.post("/api/activity/lane:lane-claude-1/close")
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body == {"closed": True}
    assert fake_client.closed == ["lane-claude-1"]
    # the other seeded records are untouched
    reg = get_activity_registry()
    assert reg.get("mcp:operator:abc") is not None
    assert reg.get("delegate:h1") is not None


@pytest.mark.asyncio
async def test_close_one_unknown_activity_is_404(tmp_path, monkeypatch) -> None:
    client, *_ = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.post("/api/activity/lane:nope/close")
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_close_one_uncancellable_kind_is_400(tmp_path, monkeypatch) -> None:
    client, *_ = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.post("/api/activity/session:keep/close")
        assert resp.status == 400
    finally:
        await client.close()


def _failed_rec(activity_id: str, kind: str) -> ActivityRecord:
    return ActivityRecord(
        activity_id=activity_id, kind=kind, label=activity_id,
        state="failed", durability="ephemeral", result="boom",
    )


@pytest.mark.asyncio
async def test_close_one_dismisses_a_failed_routine(tmp_path, monkeypatch) -> None:
    """2026-07-05: a failed routine has no cancellable kind, but 'close' on
    a failed record always means dismiss (remove-from-registry) — the
    operator-visible failure isn't lost, it's just acknowledged."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    reset_background_bus()
    reg = get_activity_registry()
    reg.register(_failed_rec("routine:run-1", "routine"))

    app = web.Application()
    activity_control.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/activity/routine:run-1/close")
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body == {"closed": True}
    assert reg.get("routine:run-1") is None


@pytest.mark.asyncio
async def test_close_all_dismisses_failed_routine_and_autonomy_chips(tmp_path, monkeypatch) -> None:
    client, fake_client, fake_mcp, fake_spawns = await _make_client(tmp_path, monkeypatch)
    reg = get_activity_registry()
    reg.register(_failed_rec("routine:run-2", "routine"))
    reg.register(_failed_rec("autonomy:item-2", "autonomy"))
    try:
        resp = await client.post("/api/activity/close-all")
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert "routine:run-2" in body["closed"]
    assert "autonomy:item-2" in body["closed"]
    assert "routine:run-2" not in body["skipped"]
    assert "autonomy:item-2" not in body["skipped"]
    assert reg.get("routine:run-2") is None
    assert reg.get("autonomy:item-2") is None
    # a still-running, non-cancellable kind stays skipped as before
    assert body["skipped"] == ["session:keep"]
