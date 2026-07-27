"""REST unpause endpoint — operator-auth + happy-path + idempotency."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.orchestrator.autonomy import PauseStore
from tesseract.orchestrator.autonomy.governor import (
    DETECTOR_LOOP,
    REASON_LOOP_DETECTED,
)
from tesseract.orchestrator.autonomy.models import AgendaSource


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


async def _make_client() -> TestClient:
    from tesseract.mirror.server.routes import agenda as agenda_route
    app = web.Application()
    app["server_sessions"] = {}
    agenda_route.register(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


def _inject_operator_session(app: web.Application, sid: str = "sess_op") -> None:
    fake = SimpleNamespace(
        chat_session=SimpleNamespace(ask_fn=lambda *a, **kw: True),
    )
    app["server_sessions"][sid] = fake


@pytest.mark.asyncio
async def test_unpause_anonymous_rejected(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.post("/api/agenda/sources/self_reflection/unpause", json={})
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unpause_unknown_source_rejected(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        resp = await client.post(
            "/api/agenda/sources/bogus/unpause",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unpause_clears_paused_source(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    # The route's auto-registered PauseStore is the one we mutate.
    store: PauseStore = client.app["autonomy_pause_store"]
    store.add(
        AgendaSource.SELF_REFLECTION,
        detector=DETECTOR_LOOP,
        reason=REASON_LOOP_DETECTED,
    )
    try:
        resp = await client.post(
            "/api/agenda/sources/self_reflection/unpause",
            json={"session_id": "sess_op", "reason": "I disagree"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["was_paused"] is True
        assert body["source"] == "self_reflection"
        assert not store.is_paused(AgendaSource.SELF_REFLECTION)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unpause_idempotent_when_not_paused(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        resp = await client.post(
            "/api/agenda/sources/self_reflection/unpause",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["was_paused"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_source_pauses_returns_active(_isolated_home: Path) -> None:
    client = await _make_client()
    store: PauseStore = client.app["autonomy_pause_store"]
    store.add(
        AgendaSource.SELF_REFLECTION,
        detector=DETECTOR_LOOP,
        reason=REASON_LOOP_DETECTED,
    )
    try:
        resp = await client.get("/api/agenda/sources/pauses")
        assert resp.status == 200
        body = await resp.json()
        sources = {p["source"] for p in body["pauses"]}
        assert sources == {"self_reflection"}
    finally:
        await client.close()
