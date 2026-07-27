"""TC-1 — GET /api/autonomy/journal returns reverse-chronological rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.orchestrator.autonomy import journal as operator_journal


async def _make_client() -> TestClient:
    from tesseract.mirror.server.routes import autonomy as autonomy_route
    app = web.Application()
    autonomy_route.register(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_empty_returns_no_rows(isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.get("/api/autonomy/journal")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"rows": [], "limit": 50, "days": 7}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_returns_rows_reverse_chrono(isolated_home: Path) -> None:
    for i in range(3):
        operator_journal.append(
            "outcome",
            {"agenda_item_id": f"ag-{i}", "worker_id": f"wk-{i}"},
        )
    client = await _make_client()
    try:
        resp = await client.get("/api/autonomy/journal?limit=2")
        assert resp.status == 200
        body = await resp.json()
        assert body["limit"] == 2
        assert len(body["rows"]) == 2
        assert body["rows"][0]["agenda_item_id"] == "ag-2"
        assert body["rows"][1]["agenda_item_id"] == "ag-1"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_invalid_limit_falls_back(isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.get("/api/autonomy/journal?limit=not-a-number")
        assert resp.status == 200
        body = await resp.json()
        assert body["limit"] == 50
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_limit_clamped(isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.get("/api/autonomy/journal?limit=9999")
        assert resp.status == 200
        body = await resp.json()
        assert body["limit"] == 500
    finally:
        await client.close()
