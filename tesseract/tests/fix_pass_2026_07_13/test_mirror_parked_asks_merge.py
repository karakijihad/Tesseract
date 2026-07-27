"""Mirror-side merge of chat-origin + controller-origin parked asks
(Option B, 2026-07-13). Route handlers are unit-tested with a stub app
(plain dict — aiohttp handlers only call ``.get``/``match_info``/``.json``
on the request), mirroring ``tests/trio/test_asks_parked_routes.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tesseract.mirror.server.activity_subscriber import ActivitySubscriber
from tesseract.mirror.server.routes.asks_parked import decide_parked, list_parked
from tesseract.mirror.server.session_model import ParkedAsk
from tesseract.orchestrator.tars_controller.ipc_client import ControllerClientError


def _chat_parked(call_id: str, fut, *, approval_id: str, session_id: str = "s-1") -> ParkedAsk:
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


# ── list_parked merges both stores ──────────────────────────────────────────


async def test_list_parked_merges_chat_and_controller_origin():
    fut = asyncio.get_running_loop().create_future()
    app = {
        "parked_asks": {"appr-chat": _chat_parked("call-1", fut, approval_id="appr-chat")},
        "controller_parked_asks": {
            "appr-ctrl": {
                "approval_id": "appr-ctrl",
                "call_id": "tu-1",
                "session_id": "s-ctrl",
                "tool_name": "bash",
                "input_summary": "ls -la",
                "spawn_handle_id": None,
                "parked_at": "2026-07-13T00:00:00+00:00",
                "origin": "controller",
            }
        },
    }
    resp = await list_parked(_FakeRequest(app))
    items = json.loads(resp.text)["items"]
    origins = {i["approval_id"]: i["origin"] for i in items}
    assert origins == {"appr-chat": "chat", "appr-ctrl": "controller"}


async def test_list_parked_empty_when_no_stores():
    resp = await list_parked(_FakeRequest({}))
    assert json.loads(resp.text)["items"] == []


# ── decide_parked: chat-origin unchanged ────────────────────────────────────


async def test_decide_chat_origin_settles_future_directly():
    fut = asyncio.get_running_loop().create_future()
    app = {
        "parked_asks": {"appr-chat": _chat_parked("call-1", fut, approval_id="appr-chat")},
        "controller_parked_asks": {},
    }
    resp = await decide_parked(
        _FakeRequest(app, approval_id="appr-chat", body={"approved": True})
    )
    assert resp.status == 200
    assert fut.result() is True


# ── decide_parked: controller-origin relays over the subscriber's client ───


class _StubClient:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.calls: list[tuple[str, bool]] = []
        self._raise = raise_error

    async def decide_parked_ask(self, approval_id: str, approved: bool, note: str = "") -> dict:
        self.calls.append((approval_id, approved))
        if self._raise:
            raise ControllerClientError("unknown_or_settled_parked_ask: boom")
        return {"event": "ack"}


class _StubSubscriber:
    def __init__(self, client) -> None:
        self.client = client


async def test_decide_controller_origin_relays_over_subscriber():
    client = _StubClient()
    app = {
        "parked_asks": {},
        "controller_parked_asks": {
            "appr-ctrl": {"approval_id": "appr-ctrl", "origin": "controller"}
        },
        "activity_subscriber": _StubSubscriber(client),
    }
    resp = await decide_parked(
        _FakeRequest(app, approval_id="appr-ctrl", body={"approved": True})
    )
    assert resp.status == 200
    assert client.calls == [("appr-ctrl", True)]
    # Authoritative removal comes from the daemon's settled broadcast, not
    # this response — the view entry must still be present here.
    assert "appr-ctrl" in app["controller_parked_asks"]


async def test_decide_controller_origin_no_subscriber_client_503s():
    app = {
        "parked_asks": {},
        "controller_parked_asks": {
            "appr-ctrl": {"approval_id": "appr-ctrl", "origin": "controller"}
        },
        "activity_subscriber": _StubSubscriber(None),
    }
    resp = await decide_parked(
        _FakeRequest(app, approval_id="appr-ctrl", body={"approved": True})
    )
    assert resp.status == 503


async def test_decide_controller_origin_no_subscriber_at_all_503s():
    app = {
        "parked_asks": {},
        "controller_parked_asks": {
            "appr-ctrl": {"approval_id": "appr-ctrl", "origin": "controller"}
        },
    }
    resp = await decide_parked(
        _FakeRequest(app, approval_id="appr-ctrl", body={"approved": True})
    )
    assert resp.status == 503


async def test_decide_controller_origin_client_error_503s():
    client = _StubClient(raise_error=True)
    app = {
        "parked_asks": {},
        "controller_parked_asks": {
            "appr-ctrl": {"approval_id": "appr-ctrl", "origin": "controller"}
        },
        "activity_subscriber": _StubSubscriber(client),
    }
    resp = await decide_parked(
        _FakeRequest(app, approval_id="appr-ctrl", body={"approved": True})
    )
    assert resp.status == 503


async def test_decide_unknown_approval_id_404s():
    app = {"parked_asks": {}, "controller_parked_asks": {}}
    resp = await decide_parked(
        _FakeRequest(app, approval_id="nope", body={"approved": True})
    )
    assert resp.status == 404


# ── ActivitySubscriber parked-store bookkeeping ─────────────────────────────


def test_subscriber_upsert_and_remove_parked():
    store: dict = {}
    sub = ActivitySubscriber(parked_store=store)
    sub._upsert_parked(
        {
            "approval_id": "appr-1",
            "session_id": "s-1",
            "tool": "bash",
            "summary": "ls",
            "tool_use_id": "tu-1",
            "parked_at": "2026-07-13T00:00:00+00:00",
        }
    )
    assert store["appr-1"]["origin"] == "controller"
    assert store["appr-1"]["call_id"] == "tu-1"
    assert store["appr-1"]["tool_name"] == "bash"
    sub._remove_parked("appr-1")
    assert "appr-1" not in store


def test_subscriber_apply_parked_snapshot_replaces_store():
    store: dict = {"stale": {"approval_id": "stale"}}
    sub = ActivitySubscriber(parked_store=store)
    sub._apply_parked_snapshot(
        [
            {
                "approval_id": "fresh",
                "session_id": "s-1",
                "tool": "bash",
                "summary": "ls",
                "tool_use_id": "tu-1",
                "parked_at": "t0",
            }
        ]
    )
    assert "stale" not in store
    assert "fresh" in store


def test_subscriber_upsert_bad_payload_does_not_crash():
    store: dict = {}
    sub = ActivitySubscriber(parked_store=store)
    sub._upsert_parked({})  # missing approval_id
    assert store == {}


def test_subscriber_none_store_is_noop():
    sub = ActivitySubscriber()  # no parked_store — must not raise
    sub._upsert_parked({"approval_id": "x"})
    sub._apply_parked_snapshot([{"approval_id": "x"}])
    sub._remove_parked("x")
