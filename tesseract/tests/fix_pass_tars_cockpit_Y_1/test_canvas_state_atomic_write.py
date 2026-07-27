"""Y-1 — canvas-state POST is atomic and leaves no partial file.

The handler writes to ``<view>.tmp.json`` then ``os.replace`` onto the
final path. Concurrent POSTs of different payloads to the same view must
leave the final file as one valid, complete JSON document — never a
half-written blob — and must not leave the ``.tmp`` scratch file behind.
"""

from __future__ import annotations

import asyncio
import importlib
import json
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


def _blob(view: str, n: int) -> dict:
    # A payload big enough that a non-atomic writer would be observable
    # mid-write if the final path were written directly.
    return {
        "schema_version": 1,
        "view": view,
        "saved_at_utc": "2026-06-03T10:00:00Z",
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "surfaces": [],
        "tldraw_snapshot": {"blob": "x" * 50_000, "n": n},
    }


@pytest.mark.asyncio
async def test_concurrent_posts_leave_valid_file(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        await asyncio.gather(
            *(client.post("/api/canvas/tars", json=_blob("tars", n)) for n in range(20))
        )
        final = _isolated_home / "workspace" / "canvas-state" / "tars.json"
        assert final.exists()
        # Must parse as one complete document — no truncation / interleave.
        parsed = json.loads(final.read_text(encoding="utf-8"))
        assert parsed["view"] == "tars"
        assert parsed["tldraw_snapshot"]["n"] in range(20)
        # The atomic-write scratch file must not survive.
        assert not (_isolated_home / "workspace" / "canvas-state" / "tars.tmp.json").exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_overwrite_replaces_prior_state(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        await client.post("/api/canvas/tars", json=_blob("tars", 1))
        await client.post("/api/canvas/tars", json=_blob("tars", 2))
        body = await (await client.get("/api/canvas/tars")).json()
        assert body["tldraw_snapshot"]["n"] == 2
    finally:
        await client.close()
