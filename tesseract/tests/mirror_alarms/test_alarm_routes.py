"""REST coverage for the Mirror alarms panel routes.

Spins up an aiohttp app with only the alarm routes mounted and an
in-memory ``AlarmRegistry``. Verifies list/cancel/snooze round-trips and
the not-ready / not-found / bad-input error branches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import alarms as alarms_route
from tesseract.scheduler.alarm_parser import ALARM_HANDLER_DOTPATH
from tesseract.scheduler.alarms import AlarmRegistry


def _build_app(registry: AlarmRegistry | None) -> web.Application:
    app = web.Application()
    app["alarm_registry"] = registry
    app.router.add_get("/api/alarms", alarms_route.list_alarms)
    app.router.add_post("/api/alarms", alarms_route.create_alarm)
    app.router.add_delete("/api/alarms/{handle}", alarms_route.cancel_alarm)
    app.router.add_post("/api/alarms/{handle}/snooze", alarms_route.snooze_alarm)
    return app


@pytest.fixture
async def client_with_registry():
    registry = AlarmRegistry(state_file=None)
    server = TestServer(_build_app(registry))
    async with server:
        async with TestClient(server) as client:
            yield client, registry


@pytest.fixture
async def client_no_registry():
    server = TestServer(_build_app(None))
    async with server:
        async with TestClient(server) as client:
            yield client


async def test_list_empty(client_with_registry):
    client, _ = client_with_registry
    resp = await client.get("/api/alarms")
    assert resp.status == 200
    assert (await resp.json()) == {"alarms": []}


async def test_list_returns_pending(client_with_registry):
    client, registry = client_with_registry
    registry.add(
        label="lunch",
        run_at=datetime.now(timezone.utc) + timedelta(minutes=45),
        handler_dotpath=ALARM_HANDLER_DOTPATH,
        message="eat",
    )
    resp = await client.get("/api/alarms")
    body = await resp.json()
    assert resp.status == 200
    assert len(body["alarms"]) == 1
    row = body["alarms"][0]
    assert row["label"] == "lunch"
    assert row["message"] == "eat"
    assert row["recurrence"] is None


async def test_list_no_registry(client_no_registry):
    resp = await client_no_registry.get("/api/alarms")
    assert resp.status == 200
    assert (await resp.json()) == {"alarms": []}


async def test_cancel_by_label(client_with_registry):
    client, registry = client_with_registry
    registry.add(
        label="cancelme",
        run_at=datetime.now(timezone.utc) + timedelta(hours=1),
        handler_dotpath=ALARM_HANDLER_DOTPATH,
    )
    resp = await client.delete("/api/alarms/cancelme")
    assert resp.status == 200
    assert (await resp.json())["cancelled"]["label"] == "cancelme"
    assert registry.list_pending() == []


async def test_cancel_unknown(client_with_registry):
    client, _ = client_with_registry
    resp = await client.delete("/api/alarms/ghost")
    assert resp.status == 404
    body = await resp.json()
    assert "no alarm matching" in body["error"]


async def test_cancel_no_registry(client_no_registry):
    resp = await client_no_registry.delete("/api/alarms/anything")
    assert resp.status == 503


async def test_snooze_pushes_run_at(client_with_registry):
    client, registry = client_with_registry
    original = datetime.now(timezone.utc) + timedelta(minutes=2)
    registry.add(
        label="snz",
        run_at=original,
        handler_dotpath=ALARM_HANDLER_DOTPATH,
    )
    resp = await client.post("/api/alarms/snz/snooze", json={"duration": "15m"})
    assert resp.status == 200
    body = await resp.json()
    assert body["snoozed"]["label"] == "snz"
    assert registry.list_pending()[0].run_at > original


async def test_snooze_default_duration(client_with_registry):
    client, registry = client_with_registry
    registry.add(
        label="snz",
        run_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        handler_dotpath=ALARM_HANDLER_DOTPATH,
    )
    resp = await client.post("/api/alarms/snz/snooze", json={})
    assert resp.status == 200
    new_run_at = registry.list_pending()[0].run_at
    assert (new_run_at - datetime.now(timezone.utc)).total_seconds() > 9 * 60


async def test_snooze_bad_duration(client_with_registry):
    client, registry = client_with_registry
    registry.add(
        label="snz",
        run_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        handler_dotpath=ALARM_HANDLER_DOTPATH,
    )
    resp = await client.post("/api/alarms/snz/snooze", json={"duration": "nonsense"})
    assert resp.status == 400


async def test_snooze_unknown_handle(client_with_registry):
    client, _ = client_with_registry
    resp = await client.post("/api/alarms/ghost/snooze", json={"duration": "10m"})
    assert resp.status == 404


async def test_create_one_shot(client_with_registry):
    client, registry = client_with_registry
    resp = await client.post(
        "/api/alarms",
        json={"label": "standup", "when": "30m", "message": "daily standup"},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["alarm"]["label"] == "standup"
    assert body["alarm"]["message"] == "daily standup"
    pending = registry.list_pending()
    assert len(pending) == 1
    assert pending[0].label == "standup"
    assert pending[0].run_at > datetime.now(timezone.utc)


async def test_create_recurring(client_with_registry):
    client, registry = client_with_registry
    resp = await client.post(
        "/api/alarms",
        json={"label": "stretch", "when": "every 30m"},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["alarm"]["recurrence"] is not None
    assert registry.list_pending()[0].recurrence is not None


async def test_create_missing_label(client_with_registry):
    client, _ = client_with_registry
    resp = await client.post("/api/alarms", json={"when": "30m"})
    assert resp.status == 400
    assert "label" in (await resp.json())["error"]


async def test_create_missing_when(client_with_registry):
    client, _ = client_with_registry
    resp = await client.post("/api/alarms", json={"label": "x"})
    assert resp.status == 400
    assert "when" in (await resp.json())["error"]


async def test_create_unparseable_when(client_with_registry):
    client, registry = client_with_registry
    resp = await client.post(
        "/api/alarms",
        json={"label": "broken", "when": "nonsensetime"},
    )
    assert resp.status == 400
    assert "cannot parse" in (await resp.json())["error"]
    assert registry.list_pending() == []


async def test_create_duplicate_label(client_with_registry):
    client, _ = client_with_registry
    first = await client.post("/api/alarms", json={"label": "dup", "when": "1h"})
    assert first.status == 201
    second = await client.post("/api/alarms", json={"label": "dup", "when": "2h"})
    assert second.status == 409
    assert "already pending" in (await second.json())["error"]


async def test_create_no_registry(client_no_registry):
    resp = await client_no_registry.post(
        "/api/alarms",
        json={"label": "x", "when": "10m"},
    )
    assert resp.status == 503


async def test_create_invalid_json(client_with_registry):
    client, _ = client_with_registry
    resp = await client.post(
        "/api/alarms",
        data="not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
