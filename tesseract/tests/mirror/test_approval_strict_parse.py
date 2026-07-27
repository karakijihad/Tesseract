"""C1 — a permission decision must be an explicit JSON boolean.

Non-boolean truthy values (the string ``"false"``, ``1``, ``[]``) must never
resolve to approval at any decision surface: parked REST, MCP REST, or the
live WebSocket tool_response path. Regression for audit C1 (type-coercion
inverting a denial into an approval).
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from tesseract.mirror.server.approvals_parse import (
    ApprovalDecisionError,
    parse_approved,
)


# --- shared parser -------------------------------------------------------

def test_parse_accepts_json_booleans():
    assert parse_approved({"approved": True}) is True
    assert parse_approved({"approved": False}) is False


@pytest.mark.parametrize(
    "value",
    ["false", "true", "0", "1", 0, 1, [], {}, None],
)
def test_parse_rejects_non_booleans(value):
    with pytest.raises(ApprovalDecisionError):
        parse_approved({"approved": value})


def test_parse_rejects_missing_key_and_non_dict():
    with pytest.raises(ApprovalDecisionError):
        parse_approved({"wrong": 1})
    with pytest.raises(ApprovalDecisionError):
        parse_approved("nope")


# --- MCP REST decision surface ------------------------------------------

class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def resolve(self, approval_id: str, approved: bool) -> bool:
        self.calls.append((approval_id, approved))
        return True


class _FakeRequest:
    def __init__(self, app: dict, match: dict, body) -> None:
        self.app = app
        self.match_info = match
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_mcp_decision_rejects_non_boolean():
    from tesseract.mirror.server.routes.mcp_approvals import decide_approval

    async def _run():
        reg = _FakeRegistry()
        app = {"mcp_approvals": reg}
        resp = await decide_approval(
            _FakeRequest(app, {"approval_id": "a-1"}, {"approved": "false"})
        )
        assert resp.status == 400
        assert reg.calls == []  # never reached the registry

    asyncio.run(_run())


# --- live WebSocket tool_response surface --------------------------------

def test_ws_resolve_ask_ignores_non_boolean_approved():
    from tesseract.mirror.server.ws import _resolve_ask

    async def _run():
        fut = asyncio.get_running_loop().create_future()
        session = types.SimpleNamespace(
            session_id="s-1",
            pending_asks={"c-1": fut},
        )
        _resolve_ask(session, {"call_id": "c-1", "approved": "false"})
        # Malformed value must NOT settle the future as approved; it stays
        # pending and the ASK later times out to the safe deny default.
        assert not fut.done()

    asyncio.run(_run())


def test_ws_resolve_ask_accepts_real_boolean():
    from tesseract.mirror.server.ws import _resolve_ask

    async def _run():
        fut = asyncio.get_running_loop().create_future()
        session = types.SimpleNamespace(
            session_id="s-1",
            pending_asks={"c-1": fut},
        )
        _resolve_ask(session, {"call_id": "c-1", "approved": False})
        assert fut.done() and fut.result() is False

    asyncio.run(_run())


def test_ws_resolve_overage_ask_ignores_non_boolean_approved():
    # C1: the cost-overage decision is a budget gate — a truthy non-boolean
    # (e.g. "false") must not resolve it as approved and unlock spend.
    from tesseract.mirror.server.ws import _resolve_overage_ask

    async def _run():
        fut = asyncio.get_running_loop().create_future()
        session = types.SimpleNamespace(pending_overage_asks={"c-1": fut})
        _resolve_overage_ask(session, {"call_id": "c-1", "approved": "false"})
        assert not fut.done()

    asyncio.run(_run())


def test_ws_resolve_overage_ask_accepts_real_boolean():
    from tesseract.mirror.server.ws import _resolve_overage_ask

    async def _run():
        fut = asyncio.get_running_loop().create_future()
        session = types.SimpleNamespace(pending_overage_asks={"c-1": fut})
        _resolve_overage_ask(session, {"call_id": "c-1", "approved": True})
        assert fut.done() and fut.result() is True

    asyncio.run(_run())
