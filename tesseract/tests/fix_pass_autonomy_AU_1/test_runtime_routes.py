"""AU-1 S2 — POST /api/runtime/shutdown + GET /api/runtime/status.

Covers kill-switch §Tests #8 (operator UI shutdown auth gate).
"""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.supervisor.intent import IntentFile, intent_path, now_utc, write_atomic


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set TESSERACT_HOME to tmp BEFORE the route module captures it.

    The route resolves TESSERACT_HOME at call time via the imported
    constant — re-importing the paths + route modules picks up the new
    env value.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    from tesseract.mirror.server.routes import runtime as runtime_route
    importlib.reload(runtime_route)
    return tmp_path


async def _make_client() -> TestClient:
    from tesseract.mirror.server.routes import runtime as runtime_route
    app = web.Application()
    app["started_at"] = time.monotonic()
    app["server_sessions"] = {}
    runtime_route.register(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_status_returns_supervisor_state(_isolated_home: Path) -> None:
    """No supervisor running, no intent, no crash storm → all-null status."""
    client = await _make_client()
    try:
        resp = await client.get("/api/runtime/status")
        assert resp.status == 200
        body = await resp.json()
        assert body["supervisor"]["alive"] is False
        assert body["supervisor"]["pid"] is None
        assert body["intent"] is None
        assert body["crash_storm"] is None
        assert body["backend"]["uptime_seconds"] >= 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_status_reads_persisted_intent(_isolated_home: Path) -> None:
    """An intent file on disk surfaces in the status payload."""
    record = IntentFile(
        intent="restart_upgrade",
        timestamp=now_utc(),
        source="upgrade_manager",
        continuation_id="ag-test",
    )
    write_atomic(intent_path(_isolated_home), record)
    client = await _make_client()
    try:
        resp = await client.get("/api/runtime/status")
        body = await resp.json()
        assert body["intent"] is not None
        assert body["intent"]["intent"] == "restart_upgrade"
        assert body["intent"]["continuation_id"] == "ag-test"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_shutdown_anonymous_rejected(_isolated_home: Path) -> None:
    """POST without session_id → 401, no intent written."""
    client = await _make_client()
    try:
        resp = await client.post("/api/runtime/shutdown", json={})
        assert resp.status == 401
        body = await resp.json()
        assert "session_id required" in body["error"]
        assert not intent_path(_isolated_home).exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_shutdown_unknown_session_rejected(_isolated_home: Path) -> None:
    """POST with a session_id that isn't in server_sessions → 401."""
    client = await _make_client()
    try:
        resp = await client.post(
            "/api/runtime/shutdown", json={"session_id": "nonexistent"},
        )
        assert resp.status == 401
        assert not intent_path(_isolated_home).exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_shutdown_accepts_valid_session(_isolated_home: Path) -> None:
    """Valid operator session → 200 + intent.json persisted."""
    fake_session = SimpleNamespace(
        chat_session=SimpleNamespace(ask_fn=lambda *args, **kwargs: True),
    )
    client = await _make_client()
    client.app["server_sessions"]["sess_op"] = fake_session
    try:
        resp = await client.post(
            "/api/runtime/shutdown",
            json={"session_id": "sess_op", "reason": "operator clicked shutdown"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "shutting_down"
        assert body["source"] == "ui_button"

        # The route's lifecycle hook writes intent BEFORE loop.stop fires.
        # The aiohttp test client's `loop.stop` call is scheduled with
        # call_later — by the time we inspect the file, it must already
        # be on disk.
        persisted = intent_path(_isolated_home)
        assert persisted.exists()
        payload = json.loads(persisted.read_text(encoding="utf-8"))
        assert payload["intent"] == "operator_quit"
        assert payload["source"] == "ui_button"
        assert payload["reason"] == "operator clicked shutdown"
    finally:
        # The route schedules loop.stop after 0.5s — TestClient.close()
        # cancels any pending callbacks.
        await client.close()
