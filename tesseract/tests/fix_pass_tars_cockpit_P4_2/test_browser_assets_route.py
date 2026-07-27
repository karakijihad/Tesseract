from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes.browser_assets import register, _SAFE_SEG


async def _make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    app = web.Application()
    register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_serves_saved_screenshot(tmp_path, monkeypatch):
    shot = tmp_path / "browser" / "abc123" / "1.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"PNG-bytes")
    client = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.get("/api/browser-assets/abc123/1.png")
        assert resp.status == 200
        body = await resp.read()
        assert body == b"PNG-bytes"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_is_404(tmp_path, monkeypatch):
    client = await _make_client(tmp_path, monkeypatch)
    try:
        resp = await client.get("/api/browser-assets/abc123/nope.png")
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bad_segment_rejected_by_safe_seg():
    # URL-encoding of path separators may be rejected by aiohttp before routing;
    # assert the guard directly instead.
    assert not _SAFE_SEG.match("../secret")
    assert not _SAFE_SEG.match("..%2f..%2fsecret")
    assert _SAFE_SEG.match("abc123")
    assert _SAFE_SEG.match("1.png")
