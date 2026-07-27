"""CR-5 — operator clicks "Reject & message user"; the bridge sends a
templated outbound reply and the event closes.

Drives ``post_channel_gate_decision`` directly with a fake bridge whose
``_send_outbound`` records the call. Avoids booting an aiohttp app — the
route handler reads only ``request.app`` / ``request.json()`` /
``request.match_info``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from tesseract.mirror.server.routes.workspace import (
    post_channel_gate_decision,
    _REJECT_TEMPLATE,
)
from tesseract.workspace_events.events import EventStore, WorkspaceEvent


@dataclass
class _StubBridge:
    sent: list[tuple[Any, str]] = field(default_factory=list)
    _sessions: dict = field(default_factory=dict)
    name: str = "telegram"

    async def _send_outbound(self, chat_id, body, *, placeholder_id=None):
        del placeholder_id
        self.sent.append((chat_id, body))


class _StubRequest:
    def __init__(self, *, app, event_id, body):
        self.app = app
        self.match_info = {"event_id": event_id}
        self._body = body

    async def json(self):
        return self._body


class _StubApp(dict):
    pass


def _emit(store, tool="web_search"):
    from tesseract.integrations._channel_gate import args_fingerprint
    args = {"query": "x"}
    ev = WorkspaceEvent.new(
        kind="tars_post",
        source="tars",
        title=f"Channel turn paused — TARS wanted to call {tool}",
        summary="user said: hi",
        payload={
            "channel": "telegram",
            "chat_id": "99",
            "session_id": "tg_99",
            "tool": tool,
            "args": args,
            "args_hash": args_fingerprint(tool, args),
            "reason": "test",
            "approve_next_turn_ttl_s": 600,
        },
    )
    return store.append_event(ev)


def test_reject_uses_default_template_when_reply_empty(tmp_path):
    store = EventStore(tmp_path / "logs")
    bridge = _StubBridge()
    app = _StubApp({"workspace_event_store": store, "telegram_bridge": bridge})
    ev = _emit(store)

    req = _StubRequest(
        app=app,
        event_id=ev.event_id,
        body={"action": "reject_and_message"},
    )
    resp = asyncio.run(post_channel_gate_decision(req))
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload["status"] == "rejected"
    assert bridge.sent == [(99, _REJECT_TEMPLATE.format(tool="web_search"))]


def test_reject_uses_operator_reply_when_provided(tmp_path):
    store = EventStore(tmp_path / "logs")
    bridge = _StubBridge()
    app = _StubApp({"workspace_event_store": store, "telegram_bridge": bridge})
    ev = _emit(store)
    custom = "Not right now — I'll loop back later."

    req = _StubRequest(
        app=app,
        event_id=ev.event_id,
        body={"action": "reject_and_message", "reply": custom},
    )
    resp = asyncio.run(post_channel_gate_decision(req))
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload["status"] == "rejected"
    assert bridge.sent == [(99, custom)]


def test_approve_next_turn_installs_session_token(tmp_path):
    from tesseract.integrations._channel_gate import consume_approval

    store = EventStore(tmp_path / "logs")
    bridge = _StubBridge()
    # Live channel session in the bridge's _sessions map.
    class _S:
        session_id = "tg_99"
    session = _S()
    bridge._sessions[99] = session
    app = _StubApp({"workspace_event_store": store, "telegram_bridge": bridge})
    ev = _emit(store)

    req = _StubRequest(
        app=app,
        event_id=ev.event_id,
        body={"action": "approve_next_turn"},
    )
    resp = asyncio.run(post_channel_gate_decision(req))
    payload = json.loads(resp.body.decode("utf-8"))
    assert payload["status"] == "approved"
    # Token now installed: the same tool+args will auto-pass once.
    assert consume_approval(session, tool_name="web_search", args={"query": "x"}) is True


def test_approve_next_turn_handles_missing_session(tmp_path):
    """No live session for the chat_id (bot restarted, etc.) — return
    400 with a specific detail rather than crashing."""
    store = EventStore(tmp_path / "logs")
    bridge = _StubBridge()  # no sessions
    app = _StubApp({"workspace_event_store": store, "telegram_bridge": bridge})
    ev = _emit(store)

    req = _StubRequest(
        app=app,
        event_id=ev.event_id,
        body={"action": "approve_next_turn"},
    )
    resp = asyncio.run(post_channel_gate_decision(req))
    assert resp.status == 400
    payload = json.loads(resp.body.decode("utf-8"))
    assert "no live channel session" in payload["error"]


def test_invalid_action_returns_400(tmp_path):
    store = EventStore(tmp_path / "logs")
    bridge = _StubBridge()
    app = _StubApp({"workspace_event_store": store, "telegram_bridge": bridge})
    ev = _emit(store)
    req = _StubRequest(
        app=app, event_id=ev.event_id, body={"action": "explode"},
    )
    resp = asyncio.run(post_channel_gate_decision(req))
    assert resp.status == 400


def test_non_gate_event_rejected(tmp_path):
    """Acting on a tars_post that wasn't sourced by the channel gate must
    400 — the payload lacks channel/chat_id/tool and the handler can't
    safely route it."""
    store = EventStore(tmp_path / "logs")
    bridge = _StubBridge()
    app = _StubApp({"workspace_event_store": store, "telegram_bridge": bridge})
    ev = WorkspaceEvent.new(
        kind="tars_post", source="tars",
        title="manual post", summary="", payload={},
    )
    store.append_event(ev)
    req = _StubRequest(
        app=app, event_id=ev.event_id, body={"action": "approve_next_turn"},
    )
    resp = asyncio.run(post_channel_gate_decision(req))
    assert resp.status == 400
