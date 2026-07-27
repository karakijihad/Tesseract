"""TC-6 — ControllerClient end-to-end against a real daemon.

The client must auth, mint or list sessions, send user_input /
approval / cancel_worker / detach, and surface pushes through
``pushes()``. Bad token / missing daemon paths surface as
``ControllerClientError``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.tars_controller import (
    ControllerClient,
    ControllerClientError,
    ControllerDaemon,
    SessionRegistry,
    auth,
)
from tesseract.orchestrator.tars_controller.events import (
    AssistantTextEvent,
    PermissionRequestEvent,
)


CONTROLLER_ID = "ctrl-test-tui"


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
    yield daemon, token
    await daemon.stop()


# ── connect / auth ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_resolves_port_and_token_from_disk(running_daemon):
    daemon, _ = running_daemon
    async with await ControllerClient.connect() as client:
        sessions = await client.list_sessions()
        assert sessions == []


@pytest.mark.asyncio
async def test_no_port_file_raises_friendly_error(isolated_home: Path):
    with pytest.raises(ControllerClientError) as exc:
        await ControllerClient.connect()
    assert "port file" in str(exc.value).lower() or "no controller" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_bad_token_disconnects(running_daemon):
    daemon, _ = running_daemon
    # Force the client to send a wrong token explicitly.
    with pytest.raises(ControllerClientError):
        async with await ControllerClient.connect(token="wrong-token") as client:
            await client.list_sessions()


# ── session flows ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_session_mints_and_returns_attached(running_daemon):
    async with await ControllerClient.connect() as client:
        attached = await client.new_session(title="t1", origin="cli")
        assert attached["event"] == "attached"
        assert attached["session"]["title"] == "t1"
        # Subsequent list_sessions shows the new one.
        sessions = await client.list_sessions()
        sids = [s["session_id"] for s in sessions]
        assert attached["session"]["session_id"] in sids


@pytest.mark.asyncio
async def test_attach_replays_transcript(running_daemon):
    daemon, _ = running_daemon
    # Use the daemon to mint a session + append events out of band.
    async with await ControllerClient.connect() as client:
        attached = await client.new_session(origin="cli")
        sid = attached["session"]["session_id"]

        # Daemon-side: append two transcript events.
        await daemon.append_event(
            sid,
            AssistantTextEvent(session_id=sid, origin="chat", text="one"),
        )
        await daemon.append_event(
            sid,
            AssistantTextEvent(session_id=sid, origin="chat", text="two"),
        )

    # New client attach — replay should include both events.
    async with await ControllerClient.connect() as client:
        attached = await client.attach(sid, from_offset=0)
        replay = attached.get("replay_events") or []
        texts = [e.get("text") for e in replay if e.get("kind") == "assistant_text"]
        assert texts == ["one", "two"]


@pytest.mark.asyncio
async def test_user_input_await_ack_errors_on_unknown_session(running_daemon):
    # M9: await_ack surfaces the daemon's session_not_found instead of the
    # caller reporting a false success for a stale session id.
    from tesseract.orchestrator.tars_controller.ipc_client import (
        ControllerClientError,
    )

    async with await ControllerClient.connect() as client:
        with pytest.raises(ControllerClientError, match="session_not_found"):
            await client.user_input("2026-07-10-deadbeef", "hi", await_ack=True)


@pytest.mark.asyncio
async def test_user_input_await_ack_ok_on_live_session(running_daemon):
    async with await ControllerClient.connect() as client:
        attached = await client.new_session(origin="cli")
        sid = attached["session"]["session_id"]
        # A live session acks — no raise.
        await client.user_input(sid, "hello", await_ack=True)


@pytest.mark.asyncio
async def test_user_input_pushes_user_text_to_attached_client(running_daemon):
    async with await ControllerClient.connect() as client:
        attached = await client.new_session(origin="cli")
        sid = attached["session"]["session_id"]
        await client.user_input(sid, "hello tars")

        # Wait for the matching transcript_event push.
        seen = None
        for _ in range(20):
            payload = await asyncio.wait_for(client._inbox.get(), timeout=2.0)
            if payload.get("event") == "transcript_event":
                evt = payload.get("transcript_event") or {}
                if evt.get("kind") == "user_text" and evt.get("text") == "hello tars":
                    seen = evt
                    break
        assert seen is not None


# ── approval round-trip ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approval_resolves_pending_permission_future(running_daemon):
    daemon, _ = running_daemon
    async with await ControllerClient.connect() as client:
        attached = await client.new_session(origin="cli")
        sid = attached["session"]["session_id"]

        # Daemon-side: start a permission request awaiting reply.
        request_task = asyncio.create_task(
            daemon.request_permission(
                sid,
                tool="bash",
                summary="rm -rf /tmp/x",
                tool_use_id="tu-test",
                timeout_seconds=5.0,
            )
        )
        # Wait for the permission_request event to arrive at the client.
        seen = False
        for _ in range(30):
            payload = await asyncio.wait_for(client._inbox.get(), timeout=2.0)
            if payload.get("event") == "transcript_event":
                evt = payload.get("transcript_event") or {}
                if evt.get("kind") == "permission_request" and not evt.get("resolved"):
                    seen = True
                    break
        assert seen
        # Approve via client.
        await client.approval(sid, "tu-test", approved=True)
        result = await asyncio.wait_for(request_task, timeout=3.0)
        assert result is True


# ── disconnect ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detach_keeps_session_alive(running_daemon):
    async with await ControllerClient.connect() as client:
        attached = await client.new_session(origin="cli")
        sid = attached["session"]["session_id"]
        await client.detach(sid)
        # Drain ack.
        await asyncio.wait_for(client._inbox.get(), timeout=2.0)
        sessions = await client.list_sessions()
        sids = [s["session_id"] for s in sessions]
        assert sid in sids  # session persists
