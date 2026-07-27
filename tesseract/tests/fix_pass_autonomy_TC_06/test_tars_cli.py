"""TC-6 — tars CLI entry-point behavior.

Covers arg parsing, session-picker flow (no sessions → new; with
sessions → numeric pick; n → new), `--list` JSON output, and the
no-controller exit path.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.tars_controller import (
    ControllerDaemon,
    SessionRegistry,
    auth,
)
from tesseract.scripts import tars_cli


CONTROLLER_ID = "ctrl-test-cli"


@pytest.fixture
async def running_daemon(isolated_home: Path):
    token = auth.mint_token()
    auth.write_token(token)
    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    yield daemon
    await daemon.stop()


# ── arg parsing ───────────────────────────────────────────────────────


class TestArgParser:
    def test_no_args_defaults(self):
        ns = tars_cli._build_parser().parse_args([])
        assert ns.session is None
        assert ns.new is False
        assert ns.list is False

    def test_session_flag(self):
        ns = tars_cli._build_parser().parse_args(["--session", "abc"])
        assert ns.session == "abc"

    def test_new_flag(self):
        ns = tars_cli._build_parser().parse_args(["--new", "--title", "T"])
        assert ns.new is True
        assert ns.title == "T"


# ── --list ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_prints_json_and_exits_zero(
    running_daemon, capsys
):
    rc = await tars_cli._async_main(
        tars_cli._build_parser().parse_args(["--list"])
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload == []


# ── no controller ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_controller_returns_2(isolated_home: Path, capsys):
    rc = await tars_cli._async_main(
        tars_cli._build_parser().parse_args(["--list"])
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "tars:" in err


# ── picker ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_picker_creates_new_when_no_sessions(
    running_daemon, monkeypatch
):
    from tesseract.orchestrator.tars_controller.ipc_client import (
        ControllerClient,
    )

    async with await ControllerClient.connect() as client:
        attached = await tars_cli._pick_or_create_session(
            client, title="picker-test"
        )
        assert attached["event"] == "attached"
        assert attached["session"]["title"] == "picker-test"


@pytest.mark.asyncio
async def test_picker_lets_user_pick_existing_session(
    running_daemon, monkeypatch
):
    from tesseract.orchestrator.tars_controller.ipc_client import (
        ControllerClient,
    )

    async with await ControllerClient.connect() as client:
        # Seed two sessions.
        a = await client.new_session(title="alpha", origin="cli")
        b = await client.new_session(title="beta", origin="cli")
        await client.detach(a["session"]["session_id"])
        await client.detach(b["session"]["session_id"])
        # Drain acks.
        for _ in range(2):
            await asyncio.wait_for(client._inbox.get(), timeout=2.0)

        # Patch _ainput to return "2" on first call.
        answers = iter(["2"])

        async def fake_ainput(prompt: str = "") -> str:
            return next(answers)

        monkeypatch.setattr(tars_cli, "_ainput", fake_ainput)
        attached = await tars_cli._pick_or_create_session(client, title=None)
        # The pick produces an attach to one of the two sessions.
        assert attached["event"] == "attached"
        assert attached["session"]["title"] in {"alpha", "beta"}


@pytest.mark.asyncio
async def test_picker_n_creates_new(running_daemon, monkeypatch):
    from tesseract.orchestrator.tars_controller.ipc_client import (
        ControllerClient,
    )

    async with await ControllerClient.connect() as client:
        await client.new_session(title="seeded", origin="cli")

        answers = iter(["n"])

        async def fake_ainput(prompt: str = "") -> str:
            return next(answers)

        monkeypatch.setattr(tars_cli, "_ainput", fake_ainput)
        attached = await tars_cli._pick_or_create_session(
            client, title="brand-new"
        )
        assert attached["session"]["title"] == "brand-new"


# ── TuiSession stdin loop ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tui_session_user_input_sent_as_ipc(
    running_daemon, monkeypatch
):
    from tesseract.orchestrator.tars_controller.ipc_client import (
        ControllerClient,
    )
    from tesseract.orchestrator.tars_controller.renderer import TuiRenderer

    async with await ControllerClient.connect() as client:
        attached = await client.new_session(origin="cli")
        sid = attached["session"]["session_id"]
        tui = tars_cli._TuiSession(client, TuiRenderer(stream=io.StringIO()), sid)

        answers = iter(["hi tars", ":quit"])

        async def fake_ainput(prompt: str = "") -> str:
            return next(answers)

        monkeypatch.setattr(tars_cli, "_ainput", fake_ainput)
        await tui.stdin_loop()
        # Drain pushes — at least one user_text transcript_event for "hi tars".
        seen = False
        for _ in range(20):
            try:
                payload = await asyncio.wait_for(
                    client._inbox.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                break
            evt = payload.get("transcript_event") or {}
            if evt.get("kind") == "user_text" and evt.get("text") == "hi tars":
                seen = True
                break
        assert seen


@pytest.mark.asyncio
async def test_tui_session_approval_uses_cached_tool_use_id(
    running_daemon, monkeypatch
):
    from tesseract.orchestrator.tars_controller.ipc_client import (
        ControllerClient,
    )
    from tesseract.orchestrator.tars_controller.renderer import TuiRenderer

    async with await ControllerClient.connect() as client:
        attached = await client.new_session(origin="cli")
        sid = attached["session"]["session_id"]
        tui = tars_cli._TuiSession(client, TuiRenderer(stream=io.StringIO()), sid)
        # Simulate a remembered tool_use_id from a prior permission_request.
        tui.remember(
            {
                "kind": "permission_request",
                "tool_use_id": "tu-pending",
                "resolved": False,
            }
        )
        assert tui.last_tool_use_id == "tu-pending"

        answers = iter([":approve", ":quit"])

        async def fake_ainput(prompt: str = "") -> str:
            return next(answers)

        monkeypatch.setattr(tars_cli, "_ainput", fake_ainput)
        # The approval message should now be sent — set up a daemon-side
        # request_permission and ensure it resolves true.
        from tesseract.orchestrator.tars_controller.events import (
            PermissionRequestEvent,
        )

        request_task = asyncio.create_task(
            running_daemon.request_permission(
                sid,
                tool="bash",
                summary="rm -rf",
                tool_use_id="tu-pending",
                timeout_seconds=5.0,
            )
        )
        # Drain the permission_request push so the inbox is clear.
        for _ in range(20):
            payload = await asyncio.wait_for(client._inbox.get(), timeout=2.0)
            if payload.get("event") == "transcript_event":
                evt = payload.get("transcript_event") or {}
                if evt.get("kind") == "permission_request":
                    break
        await tui.stdin_loop()
        result = await asyncio.wait_for(request_task, timeout=3.0)
        assert result is True
