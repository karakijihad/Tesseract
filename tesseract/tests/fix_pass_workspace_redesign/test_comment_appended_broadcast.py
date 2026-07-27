"""broadcast_comment_appended fans `workspace_comment_appended` envelopes
to every attached Mirror WS session.

Mirrors `broadcast_workspace_event`'s contract: never raises, no-op when
no app/session, otherwise calls send_envelope per session with a
`workspace_comment_appended` envelope carrying the comment payload.
"""

from __future__ import annotations

from typing import Any

import pytest

from tesseract.workspace_events import WorkspaceComment
from tesseract.workspace_events.broadcast import broadcast_comment_appended


class _FakeApp:
    def __init__(self, sessions: dict[str, Any]) -> None:
        self._d = {"server_sessions": sessions}

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)


class _FakeSession:
    def __init__(self, sid: str) -> None:
        self.session_id = sid
        self.received: list[dict[str, Any]] = []


@pytest.mark.asyncio
async def test_broadcast_fans_to_all_sessions(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    s1, s2 = _FakeSession("a"), _FakeSession("b")
    app = _FakeApp({"a": s1, "b": s2})

    async def _send(sess: _FakeSession, env: dict[str, Any]) -> None:
        sess.received.append(env)

    def _make(type_: str, cat: str, sid: str, data: dict[str, Any]) -> dict[str, Any]:
        return {"type": type_, "category": cat, "session_id": sid, "data": data}

    # Reset the helper cache so the monkeypatched values take effect on the
    # first call. The module caches the resolved (make, send) tuple after
    # the first successful import; in a test we want a clean slate.
    import tesseract.workspace_events.broadcast as bc
    monkeypatch.setattr(bc, "_MIRROR_HELPERS", (_make, _send))
    monkeypatch.setattr(bc, "_MIRROR_HELPERS_FAILED", False)

    cmt = WorkspaceComment.new(event_id="evt_x", author="operator", body="hi")
    await broadcast_comment_appended(app, cmt)

    assert len(s1.received) == 1 and len(s2.received) == 1
    env = s1.received[0]
    assert env["type"] == "workspace_comment_appended"
    assert env["category"] == "workspace"
    assert env["data"]["comment_id"] == cmt.comment_id
    assert env["data"]["body"] == "hi"


@pytest.mark.asyncio
async def test_broadcast_no_sessions_is_noop() -> None:
    app = _FakeApp({})
    cmt = WorkspaceComment.new(event_id="evt_y", author="tars", body="x")
    # Must not raise even with zero sessions attached.
    await broadcast_comment_appended(app, cmt)


@pytest.mark.asyncio
async def test_broadcast_send_failure_is_swallowed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Per-session send failures must not abort the fan-out — one broken
    socket can't silence the rest of the operator's connected tabs."""
    s_ok, s_bad = _FakeSession("ok"), _FakeSession("bad")
    app = _FakeApp({"ok": s_ok, "bad": s_bad})

    async def _send(sess: _FakeSession, env: dict[str, Any]) -> None:
        if sess.session_id == "bad":
            raise RuntimeError("socket closed")
        sess.received.append(env)

    def _make(type_: str, cat: str, sid: str, data: dict[str, Any]) -> dict[str, Any]:
        return {"type": type_, "category": cat, "session_id": sid, "data": data}

    import tesseract.workspace_events.broadcast as bc
    monkeypatch.setattr(bc, "_MIRROR_HELPERS", (_make, _send))
    monkeypatch.setattr(bc, "_MIRROR_HELPERS_FAILED", False)

    cmt = WorkspaceComment.new(event_id="evt_z", author="operator", body="hi")
    await broadcast_comment_appended(app, cmt)
    assert len(s_ok.received) == 1, "good session must still receive"
