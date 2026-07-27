from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes.controller_sessions import (
    OPERATOR_FACING_ORIGINS,
    controller_sessions_handler,
)


class _Rec:
    """Minimal stand-in for ControllerSessionRecord."""

    def __init__(self, sid: str, origin: str) -> None:
        self.session_id = sid
        self.origin = origin
        self.status = "active"
        self.title = None
        self.last_active_at = "2026-05-25T00:00:00.000Z"
        self.mode = "chat"


@pytest.mark.asyncio
async def test_lists_and_tags(monkeypatch):
    import tesseract.mirror.server.routes.controller_sessions as mod

    monkeypatch.setattr(
        mod,
        "_list_active_sessions",
        lambda: [_Rec("s1", "mirror"), _Rec("s2", "autonomy")],
    )
    app = web.Application()
    app.router.add_get("/api/controller/sessions", controller_sessions_handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/controller/sessions")
        assert resp.status == 200
        body = await resp.json()

    by_id = {s["session_id"]: s for s in body["sessions"]}
    assert by_id["s1"]["operator_facing"] is True
    assert by_id["s2"]["operator_facing"] is False
    assert "mirror" in OPERATOR_FACING_ORIGINS
    assert "cli" in OPERATOR_FACING_ORIGINS
    assert "autonomy" not in OPERATOR_FACING_ORIGINS


@pytest.mark.asyncio
async def test_real_record_serialization(monkeypatch, tmp_path):
    """Handler must not 500 when given a real ControllerSessionRecord
    (status is a plain str, last_active_at is an ISO string — both safe
    but this test guards against any future type change breaking JSON)."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    import tesseract.mirror.server.routes.controller_sessions as mod
    from tesseract.orchestrator.tars_controller.sessions import (
        ControllerSessionRecord,
    )

    rec = ControllerSessionRecord(
        session_id="test-real-001",
        origin="cli",
        mode="chat",
        transcript_path=str(tmp_path / "t.jsonl"),
    )

    monkeypatch.setattr(mod, "_list_active_sessions", lambda: [rec])

    app = web.Application()
    app.router.add_get("/api/controller/sessions", controller_sessions_handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/controller/sessions")
        assert resp.status == 200
        body = await resp.json()

    sessions = body["sessions"]
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == "test-real-001"
    assert s["origin"] == "cli"
    assert s["operator_facing"] is True
    assert s["status"] == "active"
    assert s["last_active_at"] is not None
    # Confirm it's a JSON-safe string (isoformat or raw str)
    assert isinstance(s["last_active_at"], str)
