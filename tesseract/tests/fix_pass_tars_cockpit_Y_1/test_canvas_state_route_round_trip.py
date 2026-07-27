"""Y-1 — ``/api/canvas/{view}`` round-trips the canvas blob per view.

A fresh view returns 404; after a POST the GET returns the exact blob;
two views persist independently (per-view isolation at the file layer).
Mirrors the AU-4 routes fixture: a per-test aiohttp app with only the
canvas route registered, ``TESSERACT_HOME`` pointed at a tmp_path so no
production state is touched.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


async def _make_client() -> TestClient:
    from tesseract.mirror.server.routes import canvas_state as canvas_state_route
    importlib.reload(canvas_state_route)
    app = web.Application()
    canvas_state_route.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _blob(view: str, *, n_shapes: int = 0) -> dict:
    return {
        "schema_version": 1,
        "view": view,
        "saved_at_utc": "2026-06-03T10:00:00Z",
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "surfaces": [],
        "tldraw_snapshot": {"shapes": list(range(n_shapes))},
    }


@pytest.mark.asyncio
async def test_get_missing_view_returns_404(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.get("/api/canvas/tars")
        assert resp.status == 404
        assert (await resp.json())["error"] == "not_found"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_then_get_round_trips(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        put = await client.post("/api/canvas/tars", json=_blob("tars", n_shapes=3))
        assert put.status == 200
        assert (await put.json())["ok"] is True

        got = await client.get("/api/canvas/tars")
        assert got.status == 200
        body = await got.json()
        assert body["view"] == "tars"
        assert body["schema_version"] == 1
        assert body["tldraw_snapshot"]["shapes"] == [0, 1, 2]
        # File landed under the isolated home, not anywhere production.
        assert (_isolated_home / "workspace" / "canvas-state" / "tars.json").exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_per_view_isolation(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        await client.post("/api/canvas/tars", json=_blob("tars", n_shapes=5))
        await client.post("/api/canvas/pulse", json=_blob("pulse", n_shapes=0))

        tars = await (await client.get("/api/canvas/tars")).json()
        pulse = await (await client.get("/api/canvas/pulse")).json()
        assert tars["tldraw_snapshot"]["shapes"] == [0, 1, 2, 3, 4]
        assert pulse["tldraw_snapshot"]["shapes"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_bad_schema_version(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        bad = _blob("tars")
        bad["schema_version"] = 2
        resp = await client.post("/api/canvas/tars", json=bad)
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid_schema"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_view_mismatch(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.post("/api/canvas/tars", json=_blob("pulse"))
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid_schema"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_view", ["../etc", "a/b", "with space", "x" * 65])
async def test_invalid_view_names_rejected(_isolated_home: Path, bad_view: str) -> None:
    client = await _make_client()
    try:
        # The router matches `{view}` as a single path segment, so traversal
        # attempts with slashes 404 at the router; charset/length rejects
        # surface as 400. Either way no file is written outside the dir.
        resp = await client.get(f"/api/canvas/{bad_view}")
        assert resp.status in (400, 404)
    finally:
        await client.close()
