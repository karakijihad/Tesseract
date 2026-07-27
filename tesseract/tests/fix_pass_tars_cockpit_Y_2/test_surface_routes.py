"""Mirror REST surface — list + operator emit_event + canvas-state merge."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.orchestrator.surfaces.store import get_surface_store


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths

    importlib.reload(tesseract.paths)
    from tesseract.orchestrator.surfaces.store import reset_surface_store

    reset_surface_store()
    yield tmp_path
    reset_surface_store()


async def _client() -> TestClient:
    from tesseract.mirror.server.routes import surfaces as surfaces_route
    from tesseract.mirror.server.routes import canvas_state as canvas_state_route

    importlib.reload(canvas_state_route)
    importlib.reload(surfaces_route)
    app = web.Application()
    surfaces_route.register(app)
    canvas_state_route.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_list_surfaces_returns_created(_isolated_home):
    sid = get_surface_store().create(type="folder", view="tars", props={"root": "/r"})
    client = await _client()
    try:
        body = await (await client.get("/api/surfaces/tars")).json()
        assert [s["id"] for s in body["surfaces"]] == [sid]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_surface_via_rest(_isolated_home):
    client = await _client()
    try:
        resp = await client.post(
            "/api/surfaces/tars", json={"type": "folder", "props": {"root": "/r"}, "title": "X"}
        )
        assert resp.status == 200
        body = await resp.json()
        sid = body["surface_id"]
        assert body["surface"]["props"]["root"] == "/r"
        assert sid in {s["id"] for s in get_surface_store().list_for_view("tars")}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_surface_missing_type_400(_isolated_home):
    client = await _client()
    try:
        resp = await client.post("/api/surfaces/tars", json={"props": {}})
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_emit_move_event_persists(_isolated_home):
    sid = get_surface_store().create(type="folder", view="tars", position={"x": 0, "y": 0})
    client = await _client()
    try:
        resp = await client.post(
            "/api/surfaces/tars/event",
            json={"surface_id": sid, "event": "moved", "detail": {"position": {"x": 50, "y": 60}}},
        )
        assert resp.status == 200
        assert get_surface_store().get(sid)["position"] == {"x": 50.0, "y": 60.0}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_update_surface_renames_title(_isolated_home):
    sid = get_surface_store().create(type="folder", view="tars", title="orig")
    client = await _client()
    try:
        resp = await client.post(f"/api/surfaces/tars/{sid}/update", json={"title": "renamed"})
        assert resp.status == 200
        assert (await resp.json())["surface"]["title"] == "renamed"
        assert get_surface_store().get(sid)["title"] == "renamed"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_update_unknown_surface_404(_isolated_home):
    client = await _client()
    try:
        resp = await client.post("/api/surfaces/tars/nope/update", json={"title": "x"})
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_emit_unknown_surface_404(_isolated_home):
    client = await _client()
    try:
        resp = await client.post(
            "/api/surfaces/tars/event",
            json={"surface_id": "nope", "event": "closed", "detail": {}},
        )
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_emit_bad_event_rejected(_isolated_home):
    client = await _client()
    try:
        resp = await client.post(
            "/api/surfaces/tars/event",
            json={"surface_id": "x", "event": "teleported", "detail": {}},
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_frontend_canvas_post_does_not_clobber_surfaces(_isolated_home):
    sid = get_surface_store().create(type="folder", view="tars")
    client = await _client()
    try:
        # Frontend saves its tldraw snapshot with surfaces:[] — must not wipe.
        resp = await client.post(
            "/api/canvas/tars",
            json={
                "schema_version": 1,
                "view": "tars",
                "saved_at_utc": "2026-06-03T00:00:00Z",
                "viewport": {"x": 0, "y": 0, "zoom": 1.0},
                "surfaces": [],
                "tldraw_snapshot": {"shapes": [1, 2]},
            },
        )
        assert resp.status == 200
        blob = await (await client.get("/api/canvas/tars")).json()
        assert [s["id"] for s in blob["surfaces"]] == [sid]
        assert blob["tldraw_snapshot"] == {"shapes": [1, 2]}
    finally:
        await client.close()
