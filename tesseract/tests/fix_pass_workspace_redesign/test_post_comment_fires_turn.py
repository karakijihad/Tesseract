"""POST /api/workspace/event/{event_id}/comment — fires workspace-reply
dispatch + broadcasts the operator comment.

The route dispatches a fire-and-forget ``dispatch_workspace_reply`` task
via ``_spawn_tracked``. We stub that function and verify:
- the operator comment is persisted and returned (201)
- broadcast fires exactly once with the appended comment
- dispatch_workspace_reply is called with event_id + comment_id
- when no sessions are attached, dispatch still fires (controller is
  session-independent — no Mirror session required)
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
from tesseract.workspace_events import EventStore, WorkspaceEvent


def _seed_event(store: EventStore) -> str:
    ev = WorkspaceEvent.new(
        kind="reflection_proposal",
        source="orchestrator",
        title="A pending row",
        summary="for the comment to attach to",
        payload={},
    )
    store.append_event(ev)
    return ev.event_id


@pytest_asyncio.fixture
async def client_factory(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = EventStore(tmp_path / "logs")
    event_id = _seed_event(store)

    app = web.Application()
    app["workspace_event_store"] = store
    app["server_sessions"] = {}
    app.router.add_post(
        "/api/workspace/event/{event_id}/comment", ws_routes.post_comment,
    )
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, store, event_id, app
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_comment_appends_and_triggers_dispatch(client_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, store, event_id, app = client_factory

    broadcast_calls: list[Any] = []
    dispatch_calls: list[dict[str, Any]] = []

    async def _fake_broadcast(app_arg: Any, comment: Any) -> None:
        broadcast_calls.append(comment)

    async def _fake_dispatch(app_arg: Any, *, event_id: str, comment_id: str,
                             event: Any, kind: str, comment_text: str,
                             config: Any = None) -> None:
        dispatch_calls.append({
            "event_id": event_id,
            "comment_id": comment_id,
            "kind": kind,
        })

    monkeypatch.setattr(
        "tesseract.workspace_events.broadcast.broadcast_comment_appended",
        _fake_broadcast,
    )
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_workspace_reply",
        _fake_dispatch,
    )

    resp = await client.post(
        f"/api/workspace/event/{event_id}/comment",
        json={"body": "what's the rationale?"},
    )
    # Yield to the event loop so the fire-and-forget task runs.
    await asyncio.sleep(0)
    assert resp.status == 201, await resp.text()
    body = await resp.json()
    assert body["author"] == "operator"
    assert body["body"] == "what's the rationale?"
    assert body["event_id"] == event_id

    # Comment is durable on disk regardless of dispatch.
    persisted = store.list_comments(event_id)
    assert len(persisted) == 1
    assert persisted[0].body == "what's the rationale?"

    # Broadcast fired exactly once with the just-appended comment.
    assert len(broadcast_calls) == 1
    assert broadcast_calls[0].event_id == event_id

    # Dispatch scheduled exactly once with the matching ids and kind.
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["event_id"] == event_id
    assert dispatch_calls[0]["comment_id"] == persisted[0].comment_id
    assert dispatch_calls[0]["kind"] == "comment"


@pytest.mark.asyncio
async def test_post_comment_fires_dispatch_without_sessions(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No attached Mirror sessions → dispatch still fires.
    Controller is session-independent — no Mirror session required."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = EventStore(tmp_path / "logs")
    event_id = _seed_event(store)
    app = web.Application()
    app["workspace_event_store"] = store
    app["server_sessions"] = {}
    app.router.add_post(
        "/api/workspace/event/{event_id}/comment", ws_routes.post_comment,
    )

    dispatch_calls: list[Any] = []

    async def _fake_dispatch(app_arg: Any, *, event_id: str, comment_id: str,
                             event: Any, kind: str, comment_text: str,
                             config: Any = None) -> None:
        dispatch_calls.append((event_id, comment_id))

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_workspace_reply",
        _fake_dispatch,
    )

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post(
            f"/api/workspace/event/{event_id}/comment", json={"body": "hi"},
        )
        assert resp.status == 201
        # Yield to event loop so the fire-and-forget task runs.
        await asyncio.sleep(0)
    finally:
        await client.close()

    # Controller is session-independent — dispatch fires even with no sessions.
    assert len(dispatch_calls) == 1, "dispatch must fire even with no Mirror sessions"
    assert len(store.list_comments(event_id)) == 1
