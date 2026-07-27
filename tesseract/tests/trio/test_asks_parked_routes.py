"""W4 — parked-ask REST routes: list across sessions, settle by approval_id,
404 on unknown/already-settled (bounded-hold semantics). M13: the app-level
dict + decision route key by the server-minted approval_id so two sessions with
the same provider call_id can't settle each other's ask."""

from __future__ import annotations

import asyncio
import json

from tesseract.mirror.server.routes.asks_parked import decide_parked, list_parked
from tesseract.mirror.server.session import ParkedAsk


def _parked(call_id: str, fut, *, approval_id: str, session_id: str = "s-1") -> ParkedAsk:
    return ParkedAsk(
        call_id=call_id,
        session_id=session_id,
        tool_name="file_write",
        input_summary="path=x.txt",
        spawn_handle_id="del-abc",
        parked_at="2026-07-10T00:00:00+00:00",
        future=fut,
        approval_id=approval_id,
    )


class _FakeRequest:
    def __init__(self, app: dict, approval_id: str | None = None, body=None) -> None:
        self.app = app
        self.match_info = {"approval_id": approval_id} if approval_id else {}
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _app_with(parked: dict) -> dict:
    # Parked asks live at APP level (survive WS/session cleanup), keyed by
    # the minted approval_id (M13).
    return {"parked_asks": parked}


def test_list_and_approve():
    async def _run():
        fut = asyncio.get_running_loop().create_future()
        app = _app_with({"appr-1": _parked("call-1", fut, approval_id="appr-1")})

        listed = await list_parked(_FakeRequest(app))
        items = json.loads(listed.text)["items"]
        assert items == [{
            "approval_id": "appr-1",
            "call_id": "call-1",
            "session_id": "s-1",
            "tool_name": "file_write",
            "input_summary": "path=x.txt",
            "spawn_handle_id": "del-abc",
            "parked_at": "2026-07-10T00:00:00+00:00",
            "origin": "chat",
        }]

        resp = await decide_parked(
            _FakeRequest(app, approval_id="appr-1", body={"approved": True})
        )
        assert resp.status == 200
        assert fut.result() is True

    asyncio.run(_run())


def test_unknown_approval_id_404s():
    async def _run():
        resp = await decide_parked(
            _FakeRequest(_app_with({}), approval_id="nope", body={"approved": True})
        )
        assert resp.status == 404

    asyncio.run(_run())


def test_already_settled_404s():
    async def _run():
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(True)
        app = _app_with({"appr-1": _parked("call-1", fut, approval_id="appr-1")})
        resp = await decide_parked(
            _FakeRequest(app, approval_id="appr-1", body={"approved": False})
        )
        assert resp.status == 404

    asyncio.run(_run())


def test_bad_body_400s():
    async def _run():
        fut = asyncio.get_running_loop().create_future()
        app = _app_with({"appr-1": _parked("call-1", fut, approval_id="appr-1")})
        resp = await decide_parked(
            _FakeRequest(app, approval_id="appr-1", body=ValueError("not json"))
        )
        assert resp.status == 400
        resp2 = await decide_parked(
            _FakeRequest(app, approval_id="appr-1", body={"wrong": 1})
        )
        assert resp2.status == 400
        assert not fut.done()

    asyncio.run(_run())


def test_non_boolean_approved_400s():
    # C1: a truthy non-boolean (e.g. the string "false") must not settle the
    # ask as approved — strict JSON boolean required at the permission boundary.
    async def _run():
        for bad in ("false", 1, 0, [], None):
            fut = asyncio.get_running_loop().create_future()
            app = _app_with({"appr-1": _parked("call-1", fut, approval_id="appr-1")})
            resp = await decide_parked(
                _FakeRequest(app, approval_id="appr-1", body={"approved": bad})
            )
            assert resp.status == 400, f"expected 400 for approved={bad!r}"
            assert not fut.done(), f"future settled for approved={bad!r}"

    asyncio.run(_run())


def test_same_call_id_two_sessions_settle_independently():
    # M13: two sessions produce the SAME opaque provider call_id. Deciding one
    # (by its approval_id) must settle only that one, not the other.
    async def _run():
        fut_a = asyncio.get_running_loop().create_future()
        fut_b = asyncio.get_running_loop().create_future()
        app = _app_with({
            "appr-a": _parked("dup-call", fut_a, approval_id="appr-a", session_id="s-A"),
            "appr-b": _parked("dup-call", fut_b, approval_id="appr-b", session_id="s-B"),
        })

        resp = await decide_parked(
            _FakeRequest(app, approval_id="appr-a", body={"approved": True})
        )
        assert resp.status == 200
        assert fut_a.result() is True
        assert not fut_b.done()  # the other session's ask is untouched

    asyncio.run(_run())
