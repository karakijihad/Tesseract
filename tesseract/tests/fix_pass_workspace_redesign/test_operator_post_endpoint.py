"""POST /api/workspace/operator-post — Workstream D composer endpoint.

Verifies the route shape (validation, event creation, broadcast, optional
dispatch). The dispatch helper is stubbed; we only check it fired with
the new event_id and kind="post".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import workspace as ws_routes
from tesseract.workspace_events import EventStore


@pytest_asyncio.fixture
async def client_factory(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = EventStore(tmp_path / "logs")
    app = web.Application()
    app["workspace_event_store"] = store

    class _SessionStub:
        session_id = "sess_test"

    app["server_sessions"] = {"sess_test": _SessionStub()}
    app.router.add_post(
        "/api/workspace/operator-post", ws_routes.post_operator_post,
    )
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, store, app
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_operator_post_creates_event_and_fires_dispatch(
    client_factory, monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, store, _app = client_factory

    broadcast_calls: list[Any] = []
    dispatch_calls: list[dict[str, Any]] = []

    async def _fake_broadcast(app_arg: Any, event: Any) -> None:
        broadcast_calls.append(event)

    async def _fake_dispatch(app_arg: Any, *, event_id: str, comment_id: str,
                             event: Any, kind: str, comment_text: str,
                             config: Any = None) -> None:
        dispatch_calls.append({"event_id": event_id, "kind": kind})

    monkeypatch.setattr(
        "tesseract.workspace_events.broadcast.broadcast_workspace_event",
        _fake_broadcast,
    )
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_workspace_reply",
        _fake_dispatch,
    )

    resp = await client.post(
        "/api/workspace/operator-post",
        json={"title": "Vault latency", "body": "look at the recent regression",
              "source": "scratchpad"},
    )
    # Yield to the event loop so the fire-and-forget task runs.
    await asyncio.sleep(0)
    assert resp.status == 201, await resp.text()
    body = await resp.json()
    assert body["kind"] == "operator_post"
    assert body["source"] == "operator"
    assert body["title"] == "Vault latency"
    assert body["payload"]["body"] == "look at the recent regression"
    assert body["payload"]["source"] == "scratchpad"
    assert body["delivered_to_tars"] is False

    events = store.list_events()
    assert len(events) == 1 and events[0].event_id == body["event_id"]

    assert len(broadcast_calls) == 1
    assert broadcast_calls[0].event_id == body["event_id"]

    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["event_id"] == body["event_id"]
    assert dispatch_calls[0]["kind"] == "post"


@pytest.mark.asyncio
async def test_operator_post_skips_dispatch_when_await_reply_false(
    client_factory, monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    client, _store, _app = client_factory

    dispatch_calls: list[Any] = []

    async def _fake_dispatch(*a, **kw) -> None:  # type: ignore[no-untyped-def]
        dispatch_calls.append((a, kw))

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_workspace_reply",
        _fake_dispatch,
    )

    resp = await client.post(
        "/api/workspace/operator-post?await_reply=false",
        json={"title": "", "body": "no immediate reply, please",
              "source": "hotkey"},
    )
    assert resp.status == 201
    assert dispatch_calls == []


@pytest.mark.asyncio
async def test_operator_post_validates_inputs(client_factory) -> None:  # type: ignore[no-untyped-def]
    client, _store, _app = client_factory

    # Missing body.
    r = await client.post(
        "/api/workspace/operator-post",
        json={"title": "x", "body": "", "source": "scratchpad"},
    )
    assert r.status == 400

    # Bad source.
    r = await client.post(
        "/api/workspace/operator-post",
        json={"title": "x", "body": "y", "source": "telepathy"},
    )
    assert r.status == 400

    # Auto-derived title when blank.
    r = await client.post(
        "/api/workspace/operator-post?await_reply=false",
        json={"title": "", "body": "first line\nsecond", "source": "voice"},
    )
    assert r.status == 201
    body = await r.json()
    assert body["title"] == "first line"
