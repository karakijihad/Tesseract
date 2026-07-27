"""AU-2 S2 — /api/health is a liveness probe.

Returns HTTP 200 throughout boot; ``state`` in the body distinguishes
``recovering`` from ``ready``. The 503-during-recovery contract was
revised after the supervisor's heartbeat confused "alive + recovering"
with "dead" and SIGKILLed the backend at 30s of recovery — 503 already
means "subsystem not wired" across other routes, so the boot probe
gets its own clean signal."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


async def _make_client(*, recovery_state: str | None) -> TestClient:
    from tesseract.mirror.server.routes.health import health

    app = web.Application()
    app["started_at"] = time.monotonic()
    if recovery_state is not None:
        app["recovery_state"] = recovery_state
    app.router.add_get("/api/health", health)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_health_default_ready_returns_200() -> None:
    """No recovery_state set → treat as ready (cold backend that booted
    without going through the recovery path still serves 200)."""
    client = await _make_client(recovery_state=None)
    try:
        resp = await client.get("/api/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["state"] == "ready"
        assert body["status"] == "ok"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_health_recovering_returns_200_with_state() -> None:
    """During recovery the probe stays 200 so the supervisor's
    heartbeat never confuses an in-flight RecoveryManager pass with a
    dead backend. The ``state`` field in the body carries the real
    status; consumers that need readiness check the body."""
    client = await _make_client(recovery_state="recovering")
    try:
        resp = await client.get("/api/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["state"] == "recovering"
        assert body["status"] == "recovering"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_health_ready_after_recovery_returns_200() -> None:
    client = await _make_client(recovery_state="ready")
    try:
        resp = await client.get("/api/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["state"] == "ready"
    finally:
        await client.close()
