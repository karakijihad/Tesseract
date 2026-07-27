"""Agenda comment thread — file-store + REST + WS contract.

Operator wanted a comment surface on awaiting-approval agenda items
("similar to the workspace") so they can ask clarifying questions
without bouncing to the chat. AU primitive: append-only JSONL per item
under <TESSERACT_HOME>/agenda/comments/.

Coverage: write/read primitive, malformed-line resilience, REST GET +
POST shape, operator-session auth gate, broadcast event fires.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import agenda as agenda_routes
from tesseract.orchestrator.autonomy.agenda_comments import (
    AgendaComment,
    MAX_BODY_CHARS,
    append_comment,
    list_comments,
)
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)
from tesseract.orchestrator.autonomy.paths import agenda_comments_path


# -- fixtures ------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _seed_item(store: AgendaStore, *, status: AgendaStatus = AgendaStatus.AWAITING_OPERATOR) -> AgendaItem:
    when = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    item = AgendaItem(
        id=mint_agenda_id("review", now=when),
        source=AgendaSource.OPERATOR,
        goal="Review pricing changes for John Doe's account",
        risk_class=RiskClass.OPERATOR_GATE,
        status=status,
        created_at=when,
        updated_at=when,
    )
    store.add(item)
    return item


def _inject_operator_session(app: web.Application, sid: str = "sess_op") -> None:
    sessions = app.setdefault("server_sessions", {})
    sessions[sid] = SimpleNamespace(
        chat_session=SimpleNamespace(ask_fn=lambda *a, **kw: True),
    )


async def _make_client() -> TestClient:
    app = web.Application()
    app["server_sessions"] = {}
    agenda_routes.register(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


# -- file-store primitive ------------------------------------------------


def test_append_then_list_round_trip(isolated_home: Path) -> None:
    a = append_comment("ag-x", role="operator", by="sess_op", body="first")
    b = append_comment("ag-x", role="operator", by="sess_op", body="second")
    out = list_comments("ag-x")
    assert [c.id for c in out] == [a.id, b.id]
    assert [c.body for c in out] == ["first", "second"]


def test_list_missing_file_returns_empty(isolated_home: Path) -> None:
    assert list_comments("ag-never-commented") == []


def test_append_rejects_empty_body(isolated_home: Path) -> None:
    with pytest.raises(ValueError):
        append_comment("ag-x", role="operator", by="sess_op", body="   ")


def test_append_rejects_oversize_body(isolated_home: Path) -> None:
    with pytest.raises(ValueError):
        append_comment("ag-x", role="operator", by="sess_op", body="a" * (MAX_BODY_CHARS + 1))


def test_append_rejects_bad_role(isolated_home: Path) -> None:
    with pytest.raises(ValueError):
        append_comment("ag-x", role="hacker", by="sess_op", body="hi")  # type: ignore[arg-type]


def test_corrupt_line_is_skipped(isolated_home: Path) -> None:
    append_comment("ag-x", role="operator", by="sess_op", body="ok")
    path = agenda_comments_path("ag-x")
    # Append a malformed line — one bad row must not blank the thread.
    with open(path, "a", encoding="utf-8") as fp:
        fp.write("{not json\n")
    append_comment("ag-x", role="operator", by="sess_op", body="after corruption")
    out = list_comments("ag-x")
    assert [c.body for c in out] == ["ok", "after corruption"]


# -- REST surface --------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_empty_for_no_comments(isolated_home: Path) -> None:
    client = await _make_client()
    try:
        store = client.app["agenda_store"]
        item = _seed_item(store)
        resp = await client.get(f"/api/agenda/{item.id}/comments")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"item_id": item.id, "comments": []}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_404_for_unknown_item(isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.get("/api/agenda/ag-2026-05-23-1200-doesnotexist/comments")
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_requires_operator_session(isolated_home: Path) -> None:
    client = await _make_client()
    try:
        store = client.app["agenda_store"]
        item = _seed_item(store)
        # No session injected → auth gate rejects.
        resp = await client.post(
            f"/api/agenda/{item.id}/comments",
            json={"session_id": "sess_op", "body": "hi"},
        )
        assert resp.status in (401, 403)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_appends_and_broadcasts(isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    broadcasts: list[tuple[str, str, dict]] = []

    class _Sess:
        session_id = "ws-sess"
        mode = "operator"

    async def _capture_send(sess, env):
        broadcasts.append((env["type"], env.get("topic", ""), env.get("payload", {})))

    # Replace the lazy helpers loader with a stub that captures envelopes
    # rather than serializing over a real WS.
    from tesseract.orchestrator.autonomy import broadcast as broadcast_mod

    def _stub_make_envelope(event_type, topic, session_id, payload):
        return {"type": event_type, "topic": topic, "session_id": session_id, "payload": payload}

    broadcast_mod._MIRROR_HELPERS = (_stub_make_envelope, _capture_send)
    client.app["server_sessions"]["ws-sess"] = _Sess()

    try:
        store = client.app["agenda_store"]
        item = _seed_item(store)
        resp = await client.post(
            f"/api/agenda/{item.id}/comments",
            json={"session_id": "sess_op", "body": "Why is the risk class operator_gate here?"},
        )
        assert resp.status == 201
        body = await resp.json()
        assert body["item_id"] == item.id
        assert body["comment"]["body"].startswith("Why is the risk class")
        assert body["comment"]["role"] == "operator"
        assert body["comment"]["by"] == "sess_op"

        # File-store side has the row
        comments = list_comments(item.id)
        assert len(comments) == 1
        assert comments[0].body.startswith("Why is the risk class")

        # Broadcast fired with the right envelope
        assert any(t == "agenda_comment_added" for (t, _, _) in broadcasts)
    finally:
        broadcast_mod._MIRROR_HELPERS = None
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_empty_body(isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        store = client.app["agenda_store"]
        item = _seed_item(store)
        resp = await client.post(
            f"/api/agenda/{item.id}/comments",
            json={"session_id": "sess_op", "body": "   "},
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_oversize_body(isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        store = client.app["agenda_store"]
        item = _seed_item(store)
        resp = await client.post(
            f"/api/agenda/{item.id}/comments",
            json={"session_id": "sess_op", "body": "x" * (MAX_BODY_CHARS + 1)},
        )
        assert resp.status == 400
    finally:
        await client.close()
