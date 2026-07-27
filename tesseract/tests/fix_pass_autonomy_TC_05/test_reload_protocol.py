"""TC-5 — `reload` IPC drain-and-reload behavior end-to-end.

Drives a real ``ControllerDaemon`` over loopback TCP. Verifies:

* the daemon's OS-level PID is unchanged across reload (no restart)
* in-flight dispatch turns drain up to ``drain_timeout_seconds``
* sessions transition idle → active around the reload
* the reload callback runs and its result fans out to attached clients
* drain timeout reports ``pending_turns`` without crashing the daemon
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from tesseract.kernel.sandbox._ipc_frames import decode_frame, encode_frame
from tesseract.orchestrator.tars_controller import (
    ControllerDaemon,
    SessionRegistry,
    auth,
)
from tesseract.orchestrator.tars_controller.events import AssistantTextEvent


CONTROLLER_ID = "ctrl-test-rrrr"


async def _open_client(daemon: ControllerDaemon) -> tuple[
    asyncio.StreamReader, asyncio.StreamWriter
]:
    host, port = daemon.address
    return await asyncio.open_connection(host, port)


async def _send(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(encode_frame(payload))
    await writer.drain()


async def _recv(
    reader: asyncio.StreamReader, *, timeout: float = 5.0
) -> dict[str, Any] | None:
    try:
        return await asyncio.wait_for(decode_frame(reader), timeout=timeout)
    except asyncio.IncompleteReadError:
        return None


async def _recv_event(
    reader: asyncio.StreamReader,
    event_name: str,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Drain pushes until one matches ``event_name``. Other events are
    ignored — tests assert the targeted event without being fragile to
    benign intermediate pushes (ack, transcript_event, session_status)."""

    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = max(0.05, deadline - asyncio.get_event_loop().time())
        payload = await _recv(reader, timeout=remaining)
        if payload is None:
            raise AssertionError(f"connection closed before {event_name}")
        if payload.get("event") == event_name:
            return payload


async def _auth_and_create(
    daemon: ControllerDaemon, token: str
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(writer, {"msg": "new_session", "mode": "chat", "origin": "cli"})
    attached = await _recv_event(reader, "attached")
    return reader, writer, attached["session"]["session_id"]


# ── reload protocol ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reload_pid_unchanged_and_callback_runs(isolated_home: Path):
    token = auth.mint_token()
    calls: list[str] = []

    async def reload_cb(target: str) -> dict[str, Any]:
        calls.append(target)
        return {"reloaded": [f"{target}: ok"], "failed": []}

    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        reload_callback=reload_cb,
        drain_timeout_seconds=5.0,
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    pid_before = os.getpid()
    try:
        reader, writer, _sid = await _auth_and_create(daemon, token)
        await _send(writer, {"msg": "reload", "target": "all"})
        push = await _recv_event(reader, "reload_complete")
        assert push["target"] == "all"
        assert push["reloaded"] == ["all: ok"]
        assert push["failed"] == []
        assert push["session_count"] == 1
        assert push["pending_turns"] == 0
        assert os.getpid() == pid_before
        assert calls == ["all"]
        writer.close()
        await writer.wait_closed()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_reload_drains_in_flight_turn(isolated_home: Path):
    """A short dispatch turn must finish before reload returns;
    `pending_turns` stays 0."""

    token = auth.mint_token()
    turn_started = asyncio.Event()
    turn_finished = asyncio.Event()

    async def dispatch_turn(record, text, daemon):  # type: ignore[no-untyped-def]
        turn_started.set()
        # Short, under drain timeout
        await asyncio.sleep(0.2)
        await daemon.append_event(
            record.session_id,
            AssistantTextEvent(
                session_id=record.session_id, origin="chat", text="ok"
            ),
        )
        turn_finished.set()

    async def reload_cb(target: str) -> dict[str, Any]:
        # If drain works, turn_finished is set BEFORE the callback runs.
        assert turn_finished.is_set(), "reload callback fired before drain finished"
        return {"reloaded": ["adapter"], "failed": []}

    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        dispatch_turn=dispatch_turn,
        reload_callback=reload_cb,
        drain_timeout_seconds=5.0,
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        reader, writer, sid = await _auth_and_create(daemon, token)
        await _send(writer, {"msg": "user_input", "session_id": sid, "text": "hi"})
        await asyncio.wait_for(turn_started.wait(), timeout=2.0)
        await _send(writer, {"msg": "reload", "target": "all"})
        push = await _recv_event(reader, "reload_complete", timeout=10.0)
        assert push["pending_turns"] == 0
        assert push["reloaded"] == ["adapter"]
        writer.close()
        await writer.wait_closed()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_reload_drain_timeout_reports_pending(isolated_home: Path):
    """A turn longer than ``drain_timeout_seconds`` leaves
    ``pending_turns >= 1`` without crashing the daemon."""

    token = auth.mint_token()
    turn_started = asyncio.Event()
    turn_release = asyncio.Event()

    async def dispatch_turn(record, text, daemon):  # type: ignore[no-untyped-def]
        turn_started.set()
        try:
            await turn_release.wait()
        finally:
            await daemon.append_event(
                record.session_id,
                AssistantTextEvent(
                    session_id=record.session_id,
                    origin="chat",
                    text="late",
                ),
            )

    async def reload_cb(target: str) -> dict[str, Any]:
        return {"reloaded": ["adapter"], "failed": []}

    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        dispatch_turn=dispatch_turn,
        reload_callback=reload_cb,
        drain_timeout_seconds=0.3,  # short, deliberately
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        reader, writer, sid = await _auth_and_create(daemon, token)
        await _send(writer, {"msg": "user_input", "session_id": sid, "text": "hi"})
        await asyncio.wait_for(turn_started.wait(), timeout=2.0)
        await _send(writer, {"msg": "reload", "target": "all"})
        push = await _recv_event(reader, "reload_complete", timeout=5.0)
        assert push["pending_turns"] >= 1
        assert push["drain_timeout_seconds"] == pytest.approx(0.3)
        # Daemon is still alive afterwards.
        await _send(writer, {"msg": "list_sessions"})
        listing = await _recv_event(reader, "session_list")
        assert isinstance(listing.get("sessions"), list)
        # Let the dangling turn finish so we don't leak it.
        turn_release.set()
        await asyncio.sleep(0.05)
        writer.close()
        await writer.wait_closed()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_reload_callback_failure_surfaces_in_failed(isolated_home: Path):
    token = auth.mint_token()

    async def reload_cb(target: str) -> dict[str, Any]:
        raise RuntimeError("boom")

    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        reload_callback=reload_cb,
        drain_timeout_seconds=1.0,
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        reader, writer, _sid = await _auth_and_create(daemon, token)
        await _send(writer, {"msg": "reload", "target": "tools"})
        push = await _recv_event(reader, "reload_complete")
        assert push["reloaded"] == []
        assert any("boom" in line for line in push["failed"])
        writer.close()
        await writer.wait_closed()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_reload_without_callback_succeeds_headless(isolated_home: Path):
    """No callback wired (TC-4 boot path where brain wiring failed) →
    reload still completes cleanly, headless marker in `reloaded`."""

    token = auth.mint_token()
    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        reload_callback=None,
        drain_timeout_seconds=1.0,
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        reader, writer, _sid = await _auth_and_create(daemon, token)
        await _send(writer, {"msg": "reload", "target": "config"})
        push = await _recv_event(reader, "reload_complete")
        assert any("headless" in line for line in push["reloaded"])
        assert push["failed"] == []
        writer.close()
        await writer.wait_closed()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_session_status_idle_then_active_around_reload(isolated_home: Path):
    """The session's registry status transitions idle → active across
    the drain. Observers get the `session_status` push in order."""

    token = auth.mint_token()

    async def reload_cb(target: str) -> dict[str, Any]:
        return {"reloaded": ["adapter"], "failed": []}

    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        reload_callback=reload_cb,
        drain_timeout_seconds=1.0,
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        reader, writer, sid = await _auth_and_create(daemon, token)
        await _send(writer, {"msg": "reload", "target": "all"})

        statuses: list[tuple[str, str]] = []
        for _ in range(10):
            payload = await _recv(reader, timeout=5.0)
            assert payload is not None
            if payload.get("event") == "session_status":
                statuses.append((payload["status"], payload.get("reason", "")))
            if payload.get("event") == "reload_complete":
                break
        assert ("idle", "reload:all") in statuses
        assert any(s[0] == "active" for s in statuses)
        # Idle must come before active.
        idle_idx = next(i for i, s in enumerate(statuses) if s[0] == "idle")
        active_idx = next(i for i, s in enumerate(statuses) if s[0] == "active")
        assert idle_idx < active_idx
        writer.close()
        await writer.wait_closed()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_reload_complete_fanout_to_other_clients(isolated_home: Path):
    """The asking client gets the push first; every other attached
    client also receives the same `reload_complete`."""

    token = auth.mint_token()

    async def reload_cb(target: str) -> dict[str, Any]:
        return {"reloaded": ["adapter"], "failed": []}

    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        reload_callback=reload_cb,
        drain_timeout_seconds=1.0,
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        # Client A creates a session; client B attaches as observer.
        reader_a, writer_a, sid = await _auth_and_create(daemon, token)
        reader_b, writer_b = await _open_client(daemon)
        await _send(writer_b, {"auth": token})
        await _send(
            writer_b,
            {"msg": "attach", "session_id": sid, "mode": "observer"},
        )
        await _recv_event(reader_b, "attached")

        # A triggers reload.
        await _send(writer_a, {"msg": "reload", "target": "all"})
        push_a = await _recv_event(reader_a, "reload_complete")
        push_b = await _recv_event(reader_b, "reload_complete", timeout=5.0)
        assert push_a["target"] == push_b["target"] == "all"
        assert push_a["reloaded"] == push_b["reloaded"]
        for w in (writer_a, writer_b):
            w.close()
            await w.wait_closed()
    finally:
        await daemon.stop()
