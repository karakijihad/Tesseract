"""AU-7 S1 shared fixtures — isolated home + minimal aiohttp client.

All tests under this folder run with ``TESSERACT_HOME=tmp_path`` so any
durable autonomy state (workers, agenda, governor pauses) lands under
the test sandbox per the project hard-rule on log isolation.
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
    """Per-test aiohttp app with the autonomy routes registered."""
    from tesseract.mirror.server.routes import autonomy as autonomy_route

    app = web.Application()
    autonomy_route.register(app)
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()
