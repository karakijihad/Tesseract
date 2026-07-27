"""AU-21 shared fixtures — isolated home + aiohttp client.

Per project hard-rule: tests MUST set ``TESSERACT_HOME=tmp_path`` BEFORE
importing/instantiating any writer so production logs stay untouched.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


@pytest.fixture
async def client(isolated_home: Path) -> TestClient:
    from tesseract.mirror.server.routes import operator_view as operator_view_route

    app = web.Application()
    operator_view_route.register(app)
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()
