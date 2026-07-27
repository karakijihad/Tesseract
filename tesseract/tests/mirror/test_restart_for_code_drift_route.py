"""POST /api/runtime/restart_for_code_drift — operator-clicked restart path.

Auth: accepts either (a) authenticated operator session OR (b) any
localhost caller. The localhost carve-out exists because the chip is
most needed during cold-boot windows when no chat session exists yet,
and Mirror binds 127.0.0.1 only (no remote attack vector). Every
accepted call is logged with source IP for audit.

Happy path writes ``intent.json {restart_upgrade}``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


class _StubChatSession:
    def __init__(self) -> None:
        self.ask_fn = lambda *a, **k: None


class _StubServerSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.chat_session = _StubChatSession()


async def _make_client(sessions: dict | None = None) -> TestClient:
    from tesseract.mirror.server.routes import runtime as runtime_route

    app = web.Application()
    app["server_sessions"] = sessions or {}
    runtime_route.register(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_accepts_localhost_caller_without_session(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TestServer binds 127.0.0.1, so the localhost gate accepts
    even an anonymous call. This is the cold-boot path the chip needs."""
    import asyncio
    real_get_loop = asyncio.get_running_loop
    original_call_later = real_get_loop().call_later

    def _noop_call_later(self, delay, callback, *args, **kwargs):
        if callable(callback) and getattr(callback, "__name__", "") == "stop":
            return None
        return original_call_later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(
        type(real_get_loop()), "call_later", _noop_call_later, raising=False,
    )

    client = await _make_client()
    try:
        resp = await client.post(
            "/api/runtime/restart_for_code_drift",
            json={},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["intent"] == "restart_upgrade"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_accepts_localhost_caller_with_stale_session(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown session_id from localhost falls through to the localhost
    gate rather than 401-ing. Frontend may hold a stale id from a prior
    backend lifetime — restart should still work."""
    import asyncio
    real_get_loop = asyncio.get_running_loop
    original_call_later = real_get_loop().call_later

    def _noop_call_later(self, delay, callback, *args, **kwargs):
        if callable(callback) and getattr(callback, "__name__", "") == "stop":
            return None
        return original_call_later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(
        type(real_get_loop()), "call_later", _noop_call_later, raising=False,
    )

    client = await _make_client()
    try:
        resp = await client.post(
            "/api/runtime/restart_for_code_drift",
            json={"session_id": "ghost"},
        )
        assert resp.status == 200
    finally:
        await client.close()


def test_localhost_helper_classifies_hosts() -> None:
    from types import SimpleNamespace
    from tesseract.mirror.server.routes.runtime import _is_localhost_request

    for host in ("127.0.0.1", "::1", "localhost"):
        req = SimpleNamespace(remote=host)
        assert _is_localhost_request(req), host

    for host in ("10.0.0.1", "192.168.1.5", "", None):
        req = SimpleNamespace(remote=host)
        assert not _is_localhost_request(req), host


@pytest.mark.asyncio
async def test_writes_restart_intent_with_continuation(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The route schedules a delayed `loop.stop()` 500ms after returning.
    # Stub the scheduling so the loop keeps serving while the test reads
    # the response and closes the client cleanly.
    import asyncio
    real_get_loop = asyncio.get_running_loop
    original_call_later = real_get_loop().call_later

    def _noop_call_later(self, delay, callback, *args, **kwargs):  # type: ignore[no-untyped-def]
        if callable(callback) and getattr(callback, "__name__", "") == "stop":
            return None
        return original_call_later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(
        type(real_get_loop()), "call_later", _noop_call_later, raising=False,
    )

    sessions = {"abc": _StubServerSession("abc")}
    client = await _make_client(sessions)
    try:
        resp = await client.post(
            "/api/runtime/restart_for_code_drift",
            json={
                "session_id": "abc",
                "head_sha": "deadbeefcafef00d",
                "reason": "test",
            },
        )
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    assert body["intent"] == "restart_upgrade"
    assert body["continuation_id"] == "code-drift-deadbeef"

    intent_file = _home / "runtime" / "intent.json"
    assert intent_file.exists()
    payload = json.loads(intent_file.read_text(encoding="utf-8"))
    assert payload["intent"] == "restart_upgrade"
    assert payload["continuation_id"] == "code-drift-deadbeef"


@pytest.mark.asyncio
async def test_dirty_only_drift_uses_dirty_continuation(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No head_sha (pure working-tree drift) → continuation falls back to
    'code-drift-dirty'."""
    import asyncio
    real_get_loop = asyncio.get_running_loop
    original_call_later = real_get_loop().call_later

    def _noop_call_later(self, delay, callback, *args, **kwargs):  # type: ignore[no-untyped-def]
        if callable(callback) and getattr(callback, "__name__", "") == "stop":
            return None
        return original_call_later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(
        type(real_get_loop()), "call_later", _noop_call_later, raising=False,
    )

    sessions = {"abc": _StubServerSession("abc")}
    client = await _make_client(sessions)
    try:
        resp = await client.post(
            "/api/runtime/restart_for_code_drift",
            json={"session_id": "abc"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert body["continuation_id"] == "code-drift-dirty"
