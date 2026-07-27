"""CV-1 — Mirror lane bridge (routes/lanes.py) driven by a stub client."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.controller_ws import APP_FACTORY_KEY
from tesseract.mirror.server.routes import lanes as lanes_route
from tesseract.orchestrator.tars_controller.ipc_client import ControllerClientError


class _StubClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.closed = False

    async def lane_named_list(self):
        return [{"name": "coder/claude", "lane_id": "lane-c", "kind": "claude"}]

    async def lane_named_ensure(self, *, name, kind, model, working_dir):
        return {"name": name, "lane_id": "lane-new", "kind": kind, "model": model}

    async def lane_status(self, lane_id):
        return {"alive": True, "busy": False, "queue_depth": 0, "last_activity_utc": "x"}

    async def lane_read(self, lane_id, cursor):
        return {"events": [{"kind": "assistant_text", "payload": {"text": "hi"}}], "next_cursor": "12", "count": 1}

    async def lane_send(self, lane_id, message):
        self.sent.append((lane_id, message))
        return {"accepted": True, "queue_depth": 0}

    async def lane_attach(self, lane_id):
        return {"lane": {"lane_id": lane_id}, "status": {"alive": True}, "recent_events": [], "next_cursor": "0"}

    async def lane_close(self, lane_id, reason="operator_close"):
        self.sent.append(("__close__", f"{lane_id}:{reason}"))
        return {"transcript_path": "t.txt", "final_status": "closed", "archived_at_utc": "x"}

    async def close(self):
        self.closed = True


async def _client(factory=None) -> TestClient:
    app = web.Application()
    if factory is not None:
        app[APP_FACTORY_KEY] = factory
    lanes_route.register(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


@pytest.mark.asyncio
async def test_trio_config_served():
    from tesseract.config.cockpit import load_trio_lanes

    c = await _client()
    try:
        body = await (await c.get("/api/lanes/trio")).json()
        names = [l["name"] for l in body["lanes"]]
        assert names == ["coder/claude", "auditor/codex"]
        # Route must serve the config-resolved model — compare against the
        # accessor, not a hardcoded id (roles are pillars; ids drift).
        assert body["lanes"][0]["model"] == load_trio_lanes()[0]["model"]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_named_list_status_read_via_stub():
    stub = _StubClient()
    c = await _client(lambda: _ready(stub))
    try:
        named = await (await c.get("/api/lanes/named")).json()
        assert named["named"][0]["lane_id"] == "lane-c"
        status = await (await c.get("/api/lanes/lane-c/status")).json()
        assert status["alive"] is True
        read = await (await c.get("/api/lanes/lane-c/read?cursor=5")).json()
        assert read["events"][0]["kind"] == "assistant_text"
        assert stub.closed  # client closed after each request
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_ensure_and_send():
    stub = _StubClient()
    c = await _client(lambda: _ready(stub))
    try:
        rec = await (await c.post("/api/lanes/named/ensure", json={
            "name": "coder/claude", "kind": "claude", "model": "claude-test-model",
        })).json()
        assert rec["record"]["lane_id"] == "lane-new"
        send = await c.post("/api/lanes/lane-c/send", json={"message": "go"})
        assert send.status == 200
        assert ("lane-c", "go") in stub.sent
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_send_rejects_empty_message():
    c = await _client(lambda: _ready(_StubClient()))
    try:
        resp = await c.post("/api/lanes/lane-c/send", json={"message": "  "})
        assert resp.status == 400
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_ensure_rejects_bad_kind():
    c = await _client(lambda: _ready(_StubClient()))
    try:
        resp = await c.post("/api/lanes/named/ensure", json={
            "name": "x", "kind": "gemini", "model": "m",
        })
        assert resp.status == 400
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_close_lane_terminates():
    stub = _StubClient()
    c = await _client(lambda: _ready(stub))
    try:
        resp = await c.post("/api/lanes/lane-c/close", json={"reason": "operator_close"})
        assert resp.status == 200
        assert ("__close__", "lane-c:operator_close") in stub.sent
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_controller_offline_returns_503():
    async def _boom():
        raise ControllerClientError("daemon down")

    c = await _client(_boom)
    try:
        resp = await c.get("/api/lanes/named")
        assert resp.status == 503
        assert (await resp.json())["error"] == "controller_offline"
    finally:
        await c.close()


async def _ready(stub):
    return stub
