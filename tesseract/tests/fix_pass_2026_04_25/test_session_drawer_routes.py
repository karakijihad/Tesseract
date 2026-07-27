"""P15X-A: end-to-end HTTP coverage for the new sessions routes.

GET /api/sessions/{id}/preview
POST /api/sessions/{id}/rename
POST /api/sessions/{id}/duplicate

Each route is exercised against a tmp `TESSERACT_HOME` so tests don't
touch the real `tesseract/sessions/`. Distributable-app Task 3 replaced
the route's import-time `_SESSIONS_DIR` constant with a call-time
`_sessions_dir()` helper (`TESSERACT_HOME/sessions`); the fixture below
points `TESSERACT_HOME` at a tmp dir and yields the resulting sessions
subdir so downstream tests are unaffected.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.brain.session_store import save_session
from tesseract.mirror.server.routes import sessions as sessions_route


def _seed(session_dir: Path, name: str = "alpha") -> Path:
    return save_session(
        session_dir,
        name,
        model="gpt-5.4-nano",
        started_at="2026-04-25T10:00:00+00:00",
        history=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
    )


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    sessions_dir = tmp_path / "sessions"
    app = web.Application()
    app["server_sessions"] = {}
    app.router.add_get("/api/sessions/{session_id}/preview", sessions_route.get_preview)
    app.router.add_post("/api/sessions/{session_id}/rename", sessions_route.post_rename)
    app.router.add_post("/api/sessions/{session_id}/duplicate", sessions_route.post_duplicate)
    server = TestServer(app)
    c = TestClient(server)
    await c.start_server()
    try:
        yield c, sessions_dir
    finally:
        await c.close()


# ── preview ───────────────────────────────────────────────


async def test_preview_returns_turns(client):
    c, tmp_path = client
    _seed(tmp_path)

    res = await c.get("/api/sessions/alpha/preview")
    assert res.status == 200
    body = await res.json()
    assert body["session_id"] == "alpha"
    assert body["model"] == "gpt-5.4-nano"
    assert [t["role"] for t in body["turns"]] == ["user", "assistant"]


async def test_preview_404_on_missing(client):
    c, _tmp_path = client
    res = await c.get("/api/sessions/ghost/preview")
    assert res.status == 404


async def test_preview_404_on_traversal(client):
    c, _tmp_path = client
    # `..` in the URL gets normalized by the router; using a slug-ish form
    # exercises the slug guard inside preview_session itself.
    res = await c.get("/api/sessions/.hidden/preview")
    assert res.status == 404


# ── rename ────────────────────────────────────────────────


async def test_rename_happy_path(client):
    c, tmp_path = client
    _seed(tmp_path, "alpha")

    res = await c.post("/api/sessions/alpha/rename", json={"new_name": "beta"})
    assert res.status == 200
    body = await res.json()
    assert body["session_id"] == "beta"
    assert (tmp_path / "beta.json").exists()
    assert not (tmp_path / "alpha.json").exists()


async def test_rename_updates_live_save_name(client):
    c, tmp_path = client
    _seed(tmp_path, "alpha")
    fake_session = SimpleNamespace(save_name="alpha")
    c.app["server_sessions"]["s1"] = fake_session

    res = await c.post("/api/sessions/alpha/rename", json={"new_name": "beta"})
    assert res.status == 200
    assert fake_session.save_name == "beta"


async def test_rename_404_when_source_missing(client):
    c, _tmp_path = client
    res = await c.post("/api/sessions/ghost/rename", json={"new_name": "alive"})
    assert res.status == 404
    body = await res.json()
    assert body["error"] == "not_found"


async def test_rename_400_on_invalid_name(client):
    c, tmp_path = client
    _seed(tmp_path, "alpha")
    res = await c.post(
        "/api/sessions/alpha/rename", json={"new_name": "../escape"}
    )
    assert res.status == 400
    assert (await res.json())["error"] == "invalid_name"


async def test_rename_409_when_dest_exists(client):
    c, tmp_path = client
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    res = await c.post("/api/sessions/alpha/rename", json={"new_name": "beta"})
    assert res.status == 409
    assert (await res.json())["error"] == "exists"


async def test_rename_400_on_invalid_json(client):
    c, tmp_path = client
    _seed(tmp_path, "alpha")
    res = await c.post(
        "/api/sessions/alpha/rename", data="not-json", headers={"Content-Type": "application/json"},
    )
    assert res.status == 400


# ── duplicate ─────────────────────────────────────────────


async def test_duplicate_happy_path(client):
    c, tmp_path = client
    _seed(tmp_path, "alpha")

    res = await c.post(
        "/api/sessions/alpha/duplicate", json={"dest_name": "alpha-copy"}
    )
    assert res.status == 200
    assert (await res.json())["session_id"] == "alpha-copy"
    assert (tmp_path / "alpha.json").exists()
    assert (tmp_path / "alpha-copy.json").exists()


async def test_duplicate_409_when_dest_exists(client):
    c, tmp_path = client
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    res = await c.post(
        "/api/sessions/alpha/duplicate", json={"dest_name": "beta"}
    )
    assert res.status == 409


# ── URL-suffix normalization (audit-2 fix) ────────────────


async def test_rename_accepts_json_suffix_in_url(client):
    """Callers may pass `<id>.json` in the URL. The route strips the
    suffix before looking up the live session so save_name is still
    updated, and the rename itself succeeds."""
    c, tmp_path = client
    _seed(tmp_path, "alpha")
    fake_session = SimpleNamespace(save_name="alpha")
    c.app["server_sessions"]["s1"] = fake_session

    res = await c.post("/api/sessions/alpha.json/rename", json={"new_name": "beta"})
    assert res.status == 200
    assert fake_session.save_name == "beta"


async def test_preview_accepts_json_suffix_in_url(client):
    c, tmp_path = client
    _seed(tmp_path, "alpha")

    res = await c.get("/api/sessions/alpha.json/preview")
    assert res.status == 200
    body = await res.json()
    assert body["session_id"] == "alpha"


async def test_duplicate_accepts_json_suffix_in_url(client):
    c, tmp_path = client
    _seed(tmp_path, "alpha")

    res = await c.post(
        "/api/sessions/alpha.json/duplicate", json={"dest_name": "alpha-copy"}
    )
    assert res.status == 200
    assert (tmp_path / "alpha-copy.json").exists()


# ── io_error → HTTP 500 ───────────────────────────────────


async def test_rename_500_on_io_error(client, monkeypatch):
    """OSError during rename surfaces as `io_error` → HTTP 500."""
    c, tmp_path = client
    _seed(tmp_path, "alpha")

    def _raise(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(sessions_route, "rename_session",
                        lambda *_a, **_k: (False, "io_error"))
    res = await c.post("/api/sessions/alpha/rename", json={"new_name": "beta"})
    assert res.status == 500
    assert (await res.json())["error"] == "io_error"


async def test_duplicate_500_on_io_error(client, monkeypatch):
    c, tmp_path = client
    _seed(tmp_path, "alpha")

    monkeypatch.setattr(sessions_route, "duplicate_session",
                        lambda *_a, **_k: (False, "io_error"))
    res = await c.post(
        "/api/sessions/alpha/duplicate", json={"dest_name": "alpha-copy"}
    )
    assert res.status == 500
    assert (await res.json())["error"] == "io_error"
