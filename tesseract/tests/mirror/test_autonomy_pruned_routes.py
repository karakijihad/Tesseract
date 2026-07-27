"""Task 3A — GET pruned ledger + POST mute/unmute source routes.

Mute/unmute drive the Governor's existing pause path
(``AutonomyKernel.pause_source`` / ``resume_source``) rather than a
parallel mechanism — these tests assert the kernel's ``_paused_sources``
cache (and the shared ``PauseStore``) actually change, not just that the
route returns 200. Mute/unmute are operator-session-gated, mirroring
``routes/agenda.py::unpause_source``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.governor import PauseStore
from tesseract.orchestrator.autonomy.kernel import AutonomyKernel
from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.autonomy.prune_ledger import (
    PruneRecord,
    PruneStage,
    record_prune,
)
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.mirror.server.routes import autonomy as autonomy_routes


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


class _StubChatSession:
    def __init__(self) -> None:
        self.ask_fn = lambda *a, **k: None


class _StubServerSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.chat_session = _StubChatSession()


_OPERATOR_SESSION_ID = "sess-operator-1"


def _seed_prune(source: AgendaSource, stage: PruneStage, goal: str) -> None:
    record_prune(
        PruneRecord(
            item_id=None,
            source=source,
            goal=goal,
            stage=stage,
            reason="test",
            ts=datetime.now(timezone.utc),
        )
    )


async def _client(*, with_kernel: bool = True) -> TestClient:
    app = web.Application()
    autonomy_routes.register(app)
    app["server_sessions"] = {
        _OPERATOR_SESSION_ID: _StubServerSession(_OPERATOR_SESSION_ID),
    }
    if with_kernel:
        pause_store = PauseStore()
        kernel = AutonomyKernel(
            agenda_store=AgendaStore(),
            worker_lane=WorkerLane({}),
            pause_store=pause_store,
        )
        app["autonomy_kernel"] = kernel
        app["autonomy_pause_store"] = pause_store
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_pruned_route_returns_records_and_counts(_home: Path) -> None:
    _seed_prune(AgendaSource.SELF_REFLECTION, PruneStage.DUPLICATE, "goal a")
    _seed_prune(AgendaSource.SELF_REFLECTION, PruneStage.LOW_VALUE, "goal b")
    _seed_prune(AgendaSource.PROVIDER_WATCH, PruneStage.CAPPED, "goal c")
    client = await _client()
    try:
        resp = await client.get("/api/autonomy/pruned?window_hours=168")
        assert resp.status == 200
        body = await resp.json()
        assert len(body["records"]) == 3
        assert body["records"][0]["goal"] == "goal c"  # newest-first
        assert body["counts"]["self_reflection"]["duplicate"] == 1
        assert body["counts"]["self_reflection"]["low_value"] == 1
        assert body["counts"]["provider_watch"]["capped"] == 1
    finally:
        await client.close()


async def test_pruned_route_defaults_window_and_clamps(_home: Path) -> None:
    client = await _client()
    try:
        resp = await client.get("/api/autonomy/pruned")
        assert resp.status == 200
        body = await resp.json()
        assert body["records"] == []
        assert body["counts"] == {}

        resp2 = await client.get("/api/autonomy/pruned?window_hours=999999")
        assert resp2.status == 200
    finally:
        await client.close()


async def test_mute_then_unmute_source_drives_kernel_pause(_home: Path) -> None:
    client = await _client()
    try:
        resp = await client.post(
            "/api/autonomy/source/self_reflection/mute",
            json={"session_id": _OPERATOR_SESSION_ID},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"source": "self_reflection", "muted": True}

        kernel: AutonomyKernel = client.app["autonomy_kernel"]
        assert kernel.is_source_paused(AgendaSource.SELF_REFLECTION) is True
        pause_store: PauseStore = client.app["autonomy_pause_store"]
        pause = pause_store.get(AgendaSource.SELF_REFLECTION)
        assert pause is not None
        assert pause.reason == "operator_muted"

        resp2 = await client.post(
            "/api/autonomy/source/self_reflection/unmute",
            json={"session_id": _OPERATOR_SESSION_ID},
        )
        assert resp2.status == 200
        body2 = await resp2.json()
        assert body2 == {"source": "self_reflection", "muted": False}
        assert kernel.is_source_paused(AgendaSource.SELF_REFLECTION) is False
        assert pause_store.get(AgendaSource.SELF_REFLECTION) is None
    finally:
        await client.close()


async def test_mute_without_session_id_returns_401(_home: Path) -> None:
    client = await _client()
    try:
        resp = await client.post("/api/autonomy/source/self_reflection/mute", json={})
        assert resp.status == 401
        kernel: AutonomyKernel = client.app["autonomy_kernel"]
        assert kernel.is_source_paused(AgendaSource.SELF_REFLECTION) is False
    finally:
        await client.close()


async def test_mute_unknown_session_returns_401(_home: Path) -> None:
    client = await _client()
    try:
        resp = await client.post(
            "/api/autonomy/source/self_reflection/mute",
            json={"session_id": "not-connected"},
        )
        assert resp.status == 401
    finally:
        await client.close()


async def test_mute_unknown_source_returns_400(_home: Path) -> None:
    client = await _client()
    try:
        resp = await client.post(
            "/api/autonomy/source/not-a-source/mute",
            json={"session_id": _OPERATOR_SESSION_ID},
        )
        assert resp.status == 400
    finally:
        await client.close()


async def test_mute_without_kernel_returns_503(_home: Path) -> None:
    client = await _client(with_kernel=False)
    try:
        resp = await client.post(
            "/api/autonomy/source/self_reflection/mute",
            json={"session_id": _OPERATOR_SESSION_ID},
        )
        assert resp.status == 503
    finally:
        await client.close()
