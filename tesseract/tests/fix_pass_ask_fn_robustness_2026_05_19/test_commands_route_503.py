"""GET /api/commands must signal "not ready, retry" via HTTP 503 when the
command registry hasn't been built yet (stage 3 of `_init_background`).

Pre-fix the route returned HTTP 200 with `{"commands": []}`, which the
frontend cached as `_loaded=true, _commands=[]` forever — every `/save`
and `/reset` was invisible until the page reloaded.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import commands as commands_route


@pytest_asyncio.fixture
async def _client_no_registry() -> TestClient:
    app = web.Application()
    # Deliberately leave `command_registry` absent — simulates the stage-3
    # gap on cold boot when the frontend mounts and fires its first
    # autocomplete fetch.
    app.router.add_get("/api/commands", commands_route.list_commands)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def _client_with_empty_registry() -> TestClient:
    app = web.Application()

    class _StubRegistry:
        def specs(self):
            return []

    app["command_registry"] = _StubRegistry()
    app.router.add_get("/api/commands", commands_route.list_commands)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_returns_503_when_registry_not_built(_client_no_registry) -> None:
    resp = await _client_no_registry.get("/api/commands")
    assert resp.status == 503, (
        f"BUG: /api/commands returned {resp.status} when registry not built — "
        f"frontend will cache `_loaded=true` with zero specs and every "
        f"`/save`/`/reset` will be invisible until page reload."
    )
    body = await resp.json()
    assert body == {"error": "registry_not_ready"}, (
        f"BUG: 503 payload drifted — frontend distinguishes by `error` key. body={body}"
    )


@pytest.mark.asyncio
async def test_returns_200_with_empty_list_when_registry_present_but_empty(
    _client_with_empty_registry,
) -> None:
    """A registry that's built-but-genuinely-empty (no tools registered) is
    still a successful response — the frontend's empty-200 retry path will
    eventually give up after backoff and report the registry as empty."""
    resp = await _client_with_empty_registry.get("/api/commands")
    assert resp.status == 200, (
        f"BUG: built-empty registry should be 200, not {resp.status}"
    )
    body = await resp.json()
    assert body == {"commands": []}
