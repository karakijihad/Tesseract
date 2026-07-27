"""WS-5 — GET /api/controller_sessions/{id} single-session status."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes.controller_sessions import (
    controller_session_status_handler,
)
from tesseract.orchestrator.tars_controller.sessions import SessionRegistry


async def _make_client() -> TestClient:
    app = web.Application()
    app.router.add_get(
        "/api/controller_sessions/{session_id}", controller_session_status_handler
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_status_returns_record(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rec = SessionRegistry().create_session(mode="chat", origin="mirror", title="t")
    client = await _make_client()
    try:
        resp = await client.get(f"/api/controller_sessions/{rec.session_id}")
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()
    assert body["session_id"] == rec.session_id
    assert body["status"] == "active"


@pytest.mark.asyncio
async def test_status_404_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    client = await _make_client()
    try:
        resp = await client.get("/api/controller_sessions/2026-05-26-deadbeef")
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_status_400_malformed_id(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    client = await _make_client()
    try:
        resp = await client.get("/api/controller_sessions/not-a-session")
        assert resp.status == 400
        body = await resp.json()
    finally:
        await client.close()
    assert body["error"] == "invalid_session_id"
