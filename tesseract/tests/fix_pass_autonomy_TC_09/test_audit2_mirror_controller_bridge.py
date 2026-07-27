"""Audit-1 M-2 — Mirror /ws/controller/{session_id} bridge.

TC-9 parity scenario 3 proved that two raw controller TCP clients
attached as observer + interactive see the same transcript stream.
That left the Mirror frontend's parity claim unproven through the
actual Mirror surface — no test ever opened a Mirror WS and verified
it received the typed transcript.

This suite drives the real ``/ws/controller/{session_id}`` aiohttp
handler with a stubbed ``ControllerClient`` so the bridge logic is
exercised end-to-end without booting a controller daemon. The stub's
``attach`` returns a synthetic ``AttachedPush`` shape; ``pushes()``
yields a mix of typed ``transcript_event`` payloads and a final
``_disconnected`` sentinel. The Mirror peer must receive:

1. One ``controller_event`` envelope per replay event (in order).
2. One ``controller_event`` envelope per live ``transcript_event``
   push (in order).
3. One ``controller_error`` envelope on connect/attach failure.
4. One ``controller_error`` (``controller_disconnected``) envelope
   when the daemon drops the IPC connection.
5. No envelope for non-transcript push kinds (ack, session_status).

The handler is the live Mirror boundary the audit-1 M-2 finding asked
for; this suite is the missing parity proof at that boundary.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.controller_ws import (
    APP_FACTORY_KEY,
    controller_ws_handler,
)
from tesseract.orchestrator.tars_controller.ipc_client import (
    ControllerClientError,
)


SESSION_ID = "sess-bridge-1"


class _StubClient:
    """Minimal ``ControllerClient``-shaped stub for the bridge handler.

    The bridge only needs ``attach``, ``pushes``, and ``close`` — we
    quack-type those. ``inbox`` is a public list the test prefills with
    push payloads; ``pushes()`` drains it then waits on ``done_event``
    so the test can hold the stream open or end it deterministically.
    """

    def __init__(
        self,
        *,
        replay_events: list[dict[str, Any]] | None = None,
        inbox: list[dict[str, Any]] | None = None,
        attach_error: Exception | None = None,
        end_with_disconnect: bool = True,
    ) -> None:
        self._replay = replay_events or []
        self._inbox: list[dict[str, Any]] = list(inbox or [])
        self._attach_error = attach_error
        self._end_with_disconnect = end_with_disconnect
        self._closed = False
        self.attach_called_with: tuple[str, str, int] | None = None
        self.close_called = False

    async def attach(
        self,
        session_id: str,
        *,
        mode: str = "interactive",
        from_offset: int = 0,
    ) -> dict[str, Any]:
        self.attach_called_with = (session_id, mode, from_offset)
        if self._attach_error is not None:
            raise self._attach_error
        return {
            "push": True,
            "event": "attached",
            "session": {"session_id": session_id, "mode": "observer"},
            "replay_events": list(self._replay),
            "end_offset": len(self._replay),
        }

    async def pushes(self) -> AsyncIterator[dict[str, Any]]:
        for payload in self._inbox:
            yield payload
        if self._end_with_disconnect:
            yield {"event": "_disconnected"}

    async def close(self) -> None:
        self.close_called = True
        self._closed = True


def _typed_event(kind: str, **fields: Any) -> dict[str, Any]:
    """Build a controller transcript event payload like the real
    ``events.py`` serializers emit."""
    payload: dict[str, Any] = {
        "event_id": f"ev-{kind}",
        "session_id": SESSION_ID,
        "ts": "2026-05-24T00:00:00+00:00",
        "kind": kind,
        "origin": "chat",
    }
    payload.update(fields)
    return payload


async def _build_app_with_factory(
    factory_returns: _StubClient | None = None,
    *,
    factory_raises: Exception | None = None,
) -> web.Application:
    app = web.Application()
    app.router.add_get(
        "/ws/controller/{session_id}", controller_ws_handler
    )

    async def _factory() -> Any:
        if factory_raises is not None:
            raise factory_raises
        assert factory_returns is not None
        return factory_returns

    app[APP_FACTORY_KEY] = _factory
    return app


async def _drain_envelopes(
    ws: Any, *, count: int, timeout: float = 2.0,
) -> list[dict[str, Any]]:
    """Pull ``count`` JSON envelopes from the Mirror WS."""
    envelopes: list[dict[str, Any]] = []
    for _ in range(count):
        msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        envelopes.append(msg)
    return envelopes


@pytest.mark.asyncio
async def test_bridge_forwards_replay_and_live_events_as_controller_envelopes() -> None:
    replay = [
        _typed_event("user_text", text="hi"),
        _typed_event("assistant_text", text="hello"),
    ]
    live = [
        {
            "push": True,
            "event": "transcript_event",
            "session_id": SESSION_ID,
            "transcript_event": _typed_event("assistant_text", text="live-1"),
            "end_offset": 3,
        },
        {
            "push": True,
            "event": "transcript_event",
            "session_id": SESSION_ID,
            "transcript_event": _typed_event(
                "tool_use", tool="memory_search", input={"q": "x"},
            ),
            "end_offset": 4,
        },
    ]
    stub = _StubClient(replay_events=replay, inbox=live)
    app = await _build_app_with_factory(stub)

    async with TestServer(app) as server, TestClient(server) as client:
        async with client.ws_connect(f"/ws/controller/{SESSION_ID}") as ws:
            envelopes = await _drain_envelopes(ws, count=5)

    # 2 replay + 2 live + 1 disconnect sentinel.
    assert len(envelopes) == 5
    assert stub.attach_called_with == (SESSION_ID, "observer", 0)
    assert stub.close_called is True

    forwarded_kinds = [e["data"].get("kind") for e in envelopes[:4]]
    assert forwarded_kinds == [
        "user_text", "assistant_text", "assistant_text", "tool_use",
    ]
    for env in envelopes[:4]:
        assert env["type"] == "controller_event"
        assert env["category"] == "controller"
        assert env["session_id"] == SESSION_ID
        assert env["data"]["session_id"] == SESSION_ID

    # Live event payload is forwarded verbatim from ``push.transcript_event``.
    assert envelopes[2]["data"]["text"] == "live-1"
    assert envelopes[3]["data"]["tool"] == "memory_search"

    # Disconnect surfaces as a typed error envelope so the frontend can react.
    disconnect = envelopes[4]
    assert disconnect["type"] == "controller_error"
    assert disconnect["data"]["code"] == "controller_disconnected"


@pytest.mark.asyncio
async def test_bridge_emits_connect_failed_when_factory_raises() -> None:
    app = await _build_app_with_factory(
        factory_raises=ControllerClientError(
            "no controller port file at /tmp/port; is the daemon running?"
        )
    )
    async with TestServer(app) as server, TestClient(server) as client:
        async with client.ws_connect(f"/ws/controller/{SESSION_ID}") as ws:
            env = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            close_msg = await ws.receive()

    assert env["type"] == "controller_error"
    assert env["category"] == "controller"
    assert env["data"]["code"] == "connect_failed"
    assert "no controller port file" in env["data"]["detail"]
    # WS closed cleanly after the error envelope.
    from aiohttp import WSMsgType
    assert close_msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)


@pytest.mark.asyncio
async def test_bridge_emits_attach_failed_when_attach_raises() -> None:
    stub = _StubClient(
        attach_error=ControllerClientError(
            "unknown session: sess-bridge-1"
        ),
    )
    app = await _build_app_with_factory(stub)
    async with TestServer(app) as server, TestClient(server) as client:
        async with client.ws_connect(f"/ws/controller/{SESSION_ID}") as ws:
            env = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            close_msg = await ws.receive()

    assert env["type"] == "controller_error"
    assert env["data"]["code"] == "attach_failed"
    assert "unknown session" in env["data"]["detail"]
    assert stub.close_called is True
    from aiohttp import WSMsgType
    assert close_msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)


@pytest.mark.asyncio
async def test_bridge_drops_non_transcript_push_kinds() -> None:
    """ack / session_status / reload_complete pushes never reach the
    Mirror WS — they belong to other Mirror signal paths."""
    live = [
        {"push": True, "event": "ack", "msg": "user_input"},
        {
            "push": True,
            "event": "session_status",
            "session_id": SESSION_ID,
            "status": "idle",
        },
        {
            "push": True,
            "event": "transcript_event",
            "session_id": SESSION_ID,
            "transcript_event": _typed_event("assistant_text", text="only-me"),
            "end_offset": 1,
        },
    ]
    stub = _StubClient(inbox=live)
    app = await _build_app_with_factory(stub)

    async with TestServer(app) as server, TestClient(server) as client:
        async with client.ws_connect(f"/ws/controller/{SESSION_ID}") as ws:
            # Expect only the transcript_event + disconnect sentinel.
            envelopes = await _drain_envelopes(ws, count=2)

    assert envelopes[0]["type"] == "controller_event"
    assert envelopes[0]["data"]["text"] == "only-me"
    assert envelopes[1]["type"] == "controller_error"
    assert envelopes[1]["data"]["code"] == "controller_disconnected"
