"""mirror-multi-chat P1 — chat-CRUD REST routes (over chat_store).

Routes are session-agnostic (`/api/chats/*`): chats persist across WS
connections, while ServerSession.session_id is per-connection ephemeral, so
scoping the library by session_id would be wrong. Create is WS-only (a REST
create would orphan a disk chat with no live ChatSession) — not tested here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server import chat_store
from tesseract.mirror.server.chat_store import ChatRecord
from tesseract.mirror.server.routes import chats as chats_route


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _seed(chat_id: str, *, title: str = "chat", archived: bool = False) -> None:
    chat_store.save_chat(ChatRecord(
        chat_id=chat_id,
        session_id="test-session",
        title=title,
        created_at="2026-06-27T09:00:00+00:00",
        started_at="2026-06-27T09:00:00+00:00",
        archived=archived,
        history=[{"role": "user", "content": "hi"}],
    ))


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_get("/api/chats", chats_route.list_chats_handler)
    app.router.add_get("/api/chats/{chat_id}", chats_route.get_chat_handler)
    app.router.add_patch("/api/chats/{chat_id}", chats_route.rename_chat_handler)
    app.router.add_post("/api/chats/{chat_id}/archive", chats_route.archive_chat_handler)
    app.router.add_post("/api/chats/{chat_id}/restore", chats_route.restore_chat_handler)
    app.router.add_delete("/api/chats/{chat_id}", chats_route.delete_chat_handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


_A = "a" * 32
_B = "b" * 32


async def test_list_excludes_archived_unless_requested(_home: Path) -> None:
    _seed(_A, title="open")
    _seed(_B, title="gone", archived=True)
    client = await _client()
    try:
        body = await (await client.get("/api/chats")).json()
        assert {c["chat_id"] for c in body["chats"]} == {_A}
        body_all = await (await client.get("/api/chats?include_archived=1")).json()
        assert {c["chat_id"] for c in body_all["chats"]} == {_A, _B}
    finally:
        await client.close()


async def test_get_returns_history_or_404(_home: Path) -> None:
    _seed(_A)
    client = await _client()
    try:
        resp = await client.get(f"/api/chats/{_A}")
        assert resp.status == 200
        body = await resp.json()
        assert body["chat_id"] == _A
        assert body["history"][0]["content"] == "hi"
        miss = await client.get(f"/api/chats/{_B}")
        assert miss.status == 404
        bad = await client.get("/api/chats/not-a-valid-id")
        assert bad.status == 404
    finally:
        await client.close()


async def test_rename(_home: Path) -> None:
    _seed(_A, title="old")
    client = await _client()
    try:
        resp = await client.patch(f"/api/chats/{_A}", json={"title": "new"})
        assert resp.status == 200
        assert chat_store.load_chat(_A).title == "new"
        empty = await client.patch(f"/api/chats/{_A}", json={"title": "  "})
        assert empty.status == 400
        miss = await client.patch(f"/api/chats/{_B}", json={"title": "x"})
        assert miss.status == 404
    finally:
        await client.close()


async def test_rename_archived_chat_allowed(_home: Path) -> None:
    # Renaming an archived chat is intentionally allowed (operator organizing
    # the archive) — it stays archived, just gets a new title.
    _seed(_A, title="old", archived=True)
    client = await _client()
    try:
        resp = await client.patch(f"/api/chats/{_A}", json={"title": "tidied"})
        assert resp.status == 200
        rec = chat_store.load_chat(_A)
        assert rec.title == "tidied" and rec.archived is True
    finally:
        await client.close()


async def test_archive_then_restore(_home: Path) -> None:
    _seed(_A)
    client = await _client()
    try:
        resp = await client.post(f"/api/chats/{_A}/archive")
        assert resp.status == 200
        assert chat_store.load_chat(_A).archived is True
        restored = await client.post(f"/api/chats/{_A}/restore")
        assert restored.status == 200
        assert chat_store.load_chat(_A).archived is False
        miss = await client.post(f"/api/chats/{_B}/archive")
        assert miss.status == 404
    finally:
        await client.close()


async def test_restore_requires_archived(_home: Path) -> None:
    _seed(_A)
    client = await _client()
    try:
        # Restoring an open (non-archived) chat is a contract violation, not a
        # silent 200 no-op.
        conflict = await client.post(f"/api/chats/{_A}/restore")
        assert conflict.status == 409
        assert chat_store.load_chat(_A).archived is False
        miss = await client.post(f"/api/chats/{_B}/restore")
        assert miss.status == 404
    finally:
        await client.close()


async def test_delete_requires_archived_first(_home: Path) -> None:
    _seed(_A)
    client = await _client()
    try:
        # D1: hard-delete only after archive.
        conflict = await client.delete(f"/api/chats/{_A}")
        assert conflict.status == 409
        assert chat_store.load_chat(_A) is not None
        await client.post(f"/api/chats/{_A}/archive")
        ok = await client.delete(f"/api/chats/{_A}")
        assert ok.status == 200
        assert chat_store.load_chat(_A) is None
        miss = await client.delete(f"/api/chats/{_B}")
        assert miss.status == 404
    finally:
        await client.close()
