"""Route wiring — POST /api/agenda/{id}/comments fires the TARS reply.

The operator comment is appended + broadcast (existing behaviour), then a
background reply task is spawned when ``comment_reply.enabled``. The
reply runs the real :func:`dispatch_agenda_reply` against a mocked
controller dispatch that simulates the controller calling the
``agenda_comment`` tool (Option-B durability — the tool call is what
writes the ``role="agent"`` comment, not the dispatch function). Disabled
config spawns nothing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import agenda as agenda_routes
from tesseract.orchestrator.autonomy.agenda_comments import append_comment, list_comments
from tesseract.orchestrator.autonomy.agenda_reply import AgendaReplyConfig
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)
from tesseract.orchestrator.tars_controller.dispatcher import DispatchResult


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _seed_item(store: AgendaStore) -> AgendaItem:
    when = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    item = AgendaItem(
        id=mint_agenda_id("review", now=when),
        source=AgendaSource.MEMORY_SIGNAL,
        goal="Review discovery cluster",
        risk_class=RiskClass.PROPOSE,
        status=AgendaStatus.AWAITING_OPERATOR,
        created_at=when,
        updated_at=when,
    )
    store.add(item)
    return item


def _inject_operator_session(app: web.Application, sid: str = "sess_op") -> None:
    app.setdefault("server_sessions", {})[sid] = SimpleNamespace(
        chat_session=SimpleNamespace(ask_fn=lambda *a, **kw: True),
    )


async def _make_client(store: AgendaStore) -> TestClient:
    app = web.Application()
    app["server_sessions"] = {}
    app["agenda_store"] = store
    agenda_routes.register(app)
    _inject_operator_session(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


async def test_post_comment_spawns_reply_when_enabled(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AgendaStore()
    item = _seed_item(store)

    # Capture spawned coroutines so the test can await them deterministically.
    spawned: list[asyncio.Task] = []

    def _fake_spawn(app, coro, name):
        task = asyncio.ensure_future(coro)
        spawned.append(task)
        return task

    async def _fake_dispatch(prompt, **kwargs):
        # Simulate the controller calling the `agenda_comment` tool — the
        # tool call is the durable write, not this dispatch function.
        item_id = item.id
        append_comment(item_id, role="agent", by="tars", body="Approve — low risk.")
        return DispatchResult(session_id="ctl-1", saw_assistant_text=True)

    monkeypatch.setattr("tesseract.mirror.server.ws._spawn_tracked", _fake_spawn)
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _fake_dispatch,
    )

    client = await _make_client(store)
    try:
        resp = await client.post(
            f"/api/agenda/{item.id}/comments",
            json={"session_id": "sess_op", "body": "what should we do?"},
        )
        assert resp.status == 201
        await asyncio.gather(*spawned)
    finally:
        await client.close()

    thread = list_comments(item.id)
    assert [c.role for c in thread] == ["operator", "agent"]
    assert thread[1].by == "tars"
    assert "approve" in thread[1].body.lower()


async def test_post_comment_no_reply_when_disabled(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AgendaStore()
    item = _seed_item(store)

    spawned: list[asyncio.Task] = []

    def _fake_spawn(app, coro, name):
        task = asyncio.ensure_future(coro)
        spawned.append(task)
        return task

    monkeypatch.setattr("tesseract.mirror.server.ws._spawn_tracked", _fake_spawn)
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.load_agenda_reply_config",
        lambda: AgendaReplyConfig(enabled=False),
    )

    client = await _make_client(store)
    try:
        resp = await client.post(
            f"/api/agenda/{item.id}/comments",
            json={"session_id": "sess_op", "body": "no reply please"},
        )
        assert resp.status == 201
        if spawned:
            await asyncio.gather(*spawned)
    finally:
        await client.close()

    assert spawned == []
    thread = list_comments(item.id)
    assert [c.role for c in thread] == ["operator"]
