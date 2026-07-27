"""P15-D: /mode slash command parity (Mirror).

Verifies the new `cmd_mode` flips `permissions.yaml`'s `security_mode`
in-place via the central `PermissionPolicy.set_mode` and emits a
`mode_changed` envelope to all live sessions. No-op when the requested
mode equals the current mode. Unknown modes surface as `stream_error`
with severity `warning`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from tesseract.mirror.server import commands


def _build_app_with_policy(initial_mode: str = "max") -> web.Application:
    app = web.Application()
    policy = MagicMock()
    policy.mode = initial_mode

    def _set_mode(value: str) -> None:
        if value not in {"max", "standard", "headless"}:
            raise ValueError(f"unknown mode {value!r}")
        policy.mode = value

    policy.set_mode.side_effect = _set_mode
    app["config"] = SimpleNamespace(permissions=policy)
    app["sessions"] = {}
    return app


def _build_session(session_id: str = "sess-1"):
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.closed = False
    session = SimpleNamespace(
        session_id=session_id,
        ws=ws,
        event_log=MagicMock(append=MagicMock()),
    )
    return session, ws


async def test_mode_unknown_emits_stream_error_no_mutation():
    app = _build_app_with_policy("max")
    session, ws = _build_session()

    await commands.cmd_mode(app, session, "ultra")

    assert app["config"].permissions.mode == "max"
    app["config"].permissions.set_mode.assert_not_called()
    payloads = [c.args[0] for c in ws.send_json.await_args_list]
    types = [p["type"] for p in payloads]
    assert "stream_error" in types


async def test_mode_no_op_when_already_at_target():
    app = _build_app_with_policy("standard")
    session, ws = _build_session()

    await commands.cmd_mode(app, session, "standard")

    app["config"].permissions.set_mode.assert_not_called()
    payloads = [c.args[0] for c in ws.send_json.await_args_list]
    matching = [p for p in payloads if p["type"] == "mode_changed"]
    assert len(matching) == 1
    assert matching[0]["data"]["noop"] is True


async def test_mode_switch_broadcasts_to_all_live_sessions():
    app = _build_app_with_policy("max")
    session_a, ws_a = _build_session("a")
    session_b, ws_b = _build_session("b")
    app["sessions"] = {"a": ws_a, "b": ws_b}

    await commands.cmd_mode(app, session_a, "standard")

    assert app["config"].permissions.mode == "standard"
    # Both live sockets see a mode_changed envelope.
    a_types = [c.args[0]["type"] for c in ws_a.send_json.await_args_list]
    b_types = [c.args[0]["type"] for c in ws_b.send_json.await_args_list]
    assert "mode_changed" in a_types
    assert "mode_changed" in b_types


async def test_mode_case_insensitive_and_trimmed():
    app = _build_app_with_policy("max")
    session, ws = _build_session()

    await commands.cmd_mode(app, session, "  HEADLESS  ")

    assert app["config"].permissions.mode == "headless"


@pytest.mark.parametrize("bad", ["", "   ", None])
async def test_mode_empty_arg_treated_as_unknown(bad):
    app = _build_app_with_policy("max")
    session, ws = _build_session()

    await commands.cmd_mode(app, session, bad)

    app["config"].permissions.set_mode.assert_not_called()
    types = [c.args[0]["type"] for c in ws.send_json.await_args_list]
    assert "stream_error" in types
