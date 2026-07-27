"""End-to-end IPC tests against a live ``ControllerDaemon``.

Each test starts a real daemon on a kernel-assigned loopback port,
opens a TCP client, and drives the protocol. Auth, session lifecycle,
transcript fan-out, and Contract #8 detach behavior all exercised here.
"""

from __future__ import annotations

import asyncio
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


CONTROLLER_ID = "ctrl-test-aaaa"


async def _open_client(daemon: ControllerDaemon) -> tuple[
    asyncio.StreamReader, asyncio.StreamWriter
]:
    host, port = daemon.address
    return await asyncio.open_connection(host, port)


async def _send(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(encode_frame(payload))
    await writer.drain()


async def _recv(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    try:
        return await asyncio.wait_for(decode_frame(reader), timeout=5.0)
    except asyncio.IncompleteReadError:
        return None


async def _recv_skip_activity(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Drain past incidental `activity_event` broadcasts (the session's own
    activity-registry registration, fanned out by the controller's
    background activity forwarder) to the next substantive push. This file
    asserts on protocol events, not activity-registry chatter, and the two
    races against each other."""
    while True:
        msg = await _recv(reader)
        if msg is None or msg.get("event") != "activity_event":
            return msg


@pytest.fixture
async def running_daemon(isolated_home: Path):
    token = auth.mint_token()
    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        heartbeat_interval=3600,  # silence heartbeat task during tests
    )
    await daemon.start(host="127.0.0.1", port=0)
    yield daemon, token
    await daemon.stop()


@pytest.mark.asyncio
async def test_port_file_written_on_start(running_daemon) -> None:
    from tesseract.orchestrator.tars_controller import port_file_path

    daemon, _token = running_daemon
    path = port_file_path()
    assert path.exists()
    assert int(path.read_text(encoding="utf-8")) == daemon.address[1]


@pytest.mark.asyncio
async def test_auth_required_as_first_message(running_daemon) -> None:
    daemon, _token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"msg": "list_sessions"})
    reply = await _recv(reader)
    assert reply is not None
    assert reply["event"] == "error"
    assert reply["code"] == "auth_required"
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_bad_token_closes_connection(running_daemon) -> None:
    daemon, _token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": "wrong-token"})
    reply = await _recv(reader)
    assert reply is not None
    assert reply["event"] == "error"
    assert reply["code"] == "auth_failed"
    # Connection should have been closed by the daemon — next read is EOF.
    eof = await reader.read()
    assert eof == b""
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_new_session_mints_and_persists(running_daemon) -> None:
    from tesseract.orchestrator.tars_controller import sessions_dir

    daemon, token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(
        writer,
        {"msg": "new_session", "title": "smoke", "mode": "chat", "origin": "cli"},
    )
    reply = await _recv(reader)
    assert reply is not None
    assert reply["event"] == "attached"
    session_id = reply["session"]["session_id"]
    assert (sessions_dir() / f"{session_id}.json").exists()
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_user_input_persists_transcript_event_and_acks(running_daemon) -> None:
    from tesseract.orchestrator.tars_controller import transcript_path

    daemon, token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(
        writer,
        {"msg": "new_session", "title": None, "mode": "chat", "origin": "cli"},
    )
    attached = await _recv(reader)
    session_id = attached["session"]["session_id"]

    await _send(
        writer,
        {"msg": "user_input", "session_id": session_id, "text": "hello tars"},
    )
    # We get the transcript_event fan-out + an ack (order: fan-out first,
    # ack second) — possibly interleaved with an incidental
    # `activity_event` broadcast from the session-creation activity
    # registration, so read until both land rather than assuming exactly 2.
    pushes: list[dict[str, Any]] = []
    events: list[str] = []
    while "transcript_event" not in events or "ack" not in events:
        msg = await _recv(reader)
        assert msg is not None
        pushes.append(msg)
        events.append(msg["event"])
    fan_out = next(p for p in pushes if p["event"] == "transcript_event")
    assert fan_out["transcript_event"]["text"] == "hello tars"

    # Transcript JSONL was actually written.
    path = transcript_path(session_id)
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert any('"hello tars"' in ln for ln in lines)
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_attach_replays_existing_events(running_daemon) -> None:
    daemon, token = running_daemon

    # First client: create the session and append an assistant event directly.
    reader1, writer1 = await _open_client(daemon)
    await _send(writer1, {"auth": token})
    await _send(
        writer1,
        {"msg": "new_session", "title": None, "mode": "chat", "origin": "cli"},
    )
    attached1 = await _recv(reader1)
    session_id = attached1["session"]["session_id"]
    await daemon.append_event(
        session_id,
        AssistantTextEvent(
            session_id=session_id, origin="chat", text="server-side reply"
        ),
    )
    # Drain the fan-out for client 1.
    fan = await _recv_skip_activity(reader1)
    assert fan["event"] == "transcript_event"
    writer1.close()
    await writer1.wait_closed()

    # Second client: attach and expect replay.
    reader2, writer2 = await _open_client(daemon)
    await _send(writer2, {"auth": token})
    await _send(
        writer2,
        {"msg": "attach", "session_id": session_id, "mode": "observer"},
    )
    # client1's disconnect (writer1.close() above) triggers its own
    # detach -> update_session(status="detached") -> activity_event
    # broadcast, which can land on client2 before its own attach reply.
    attached2 = await _recv_skip_activity(reader2)
    assert attached2["event"] == "attached"
    replay = attached2["replay_events"]
    kinds = [ev["kind"] for ev in replay]
    assert "assistant_text" in kinds
    writer2.close()
    await writer2.wait_closed()


@pytest.mark.asyncio
async def test_attach_unknown_session_errors(running_daemon) -> None:
    daemon, token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(
        writer,
        {"msg": "attach", "session_id": "2099-01-01-deadbeef", "mode": "interactive"},
    )
    reply = await _recv(reader)
    assert reply["event"] == "error"
    assert reply["code"] == "session_not_found"
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_list_sessions_returns_registered(running_daemon) -> None:
    daemon, token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(
        writer,
        {"msg": "new_session", "title": "first", "mode": "chat", "origin": "cli"},
    )
    await _recv(reader)
    await _send(writer, {"msg": "list_sessions"})
    reply = await _recv_skip_activity(reader)
    assert reply["event"] == "session_list"
    assert len(reply["sessions"]) == 1
    assert reply["sessions"][0]["title"] == "first"
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_disconnect_marks_session_detached_not_killed(running_daemon) -> None:
    """Contract #8 parity: a client disconnect must NOT close the session."""
    daemon, token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(
        writer,
        {"msg": "new_session", "title": None, "mode": "chat", "origin": "cli"},
    )
    attached = await _recv(reader)
    session_id = attached["session"]["session_id"]
    writer.close()
    await writer.wait_closed()
    # Give the daemon a tick to process the disconnect.
    await asyncio.sleep(0.05)
    record = daemon._registry.get_session(session_id)  # noqa: SLF001 — tested intentionally
    assert record is not None
    assert record.status == "detached"


@pytest.mark.asyncio
async def test_approval_resolves_pending_future(running_daemon) -> None:
    daemon, token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(
        writer,
        {"msg": "new_session", "title": None, "mode": "chat", "origin": "cli"},
    )
    attached = await _recv(reader)
    session_id = attached["session"]["session_id"]

    # request_permission awaits a future; resolve it by sending an approval.
    pending = asyncio.create_task(
        daemon.request_permission(
            session_id,
            tool="bash",
            summary="run ls",
            tool_use_id="tu-1",
            timeout_seconds=5.0,
        )
    )
    # The permission_request event is fanned out; drain it.
    push = await _recv_skip_activity(reader)
    assert push["event"] == "transcript_event"
    assert push["transcript_event"]["kind"] == "permission_request"

    await _send(
        writer,
        {
            "msg": "approval",
            "session_id": session_id,
            "tool_use_id": "tu-1",
            "approved": True,
        },
    )
    # Drain the ack.
    ack = await _recv(reader)
    assert ack["event"] == "ack"
    approved = await pending
    assert approved is True
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_request_permission_parks_when_headless(
    running_daemon, monkeypatch
) -> None:
    """Option B (2026-07-13): no interactive client attached → the ask PARKS
    instead of an immediate deny (the old `headless_blocked` behavior). It
    settles the moment `decide_parked_ask` lands — proving the daemon didn't
    just deny it up front — and the transcript records the full
    pending → parked → resolved sequence."""
    from tesseract.orchestrator.tars_controller import TranscriptReader
    from tesseract.config import runtime_limits

    monkeypatch.setattr(runtime_limits, "load_ask_park_timeout_s", lambda p: 5.0)

    daemon, _token = running_daemon
    sid = daemon._registry.create_session(  # noqa: SLF001
        mode="autonomy", origin="autonomy", controller_id=CONTROLLER_ID
    ).session_id

    pending = asyncio.create_task(
        daemon.request_permission(
            sid,
            tool="bash",
            summary="rm -rf /",
            tool_use_id="tu-headless",
            timeout_seconds=1.0,
        )
    )
    for _ in range(200):
        if any(
            e.tool_use_id == "tu-headless" for e in daemon._parked_asks.values()  # noqa: SLF001
        ):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("headless ask never parked")

    entry = next(
        e for e in daemon._parked_asks.values() if e.tool_use_id == "tu-headless"  # noqa: SLF001
    )
    entry.future.set_result(True)
    approved = await pending
    assert approved is True

    events = [event for event, _ in TranscriptReader(sid).read_from()]
    kinds_resolutions = [
        (e.model_dump(mode="json").get("resolution"), e.model_dump(mode="json")["resolved"])
        for e in events
    ]
    assert (None, False) in kinds_resolutions  # initial pending row
    assert ("parked", False) in kinds_resolutions  # park marker row
    assert ("approved", True) in kinds_resolutions  # final resolution row


@pytest.mark.asyncio
async def test_request_permission_park_timeout_denies(
    running_daemon, monkeypatch
) -> None:
    """The park window itself is bounded — if nobody ever decides, it
    finally denies (`park_timeout`), preserving the no-forever-hang
    property of the old `headless_blocked` path."""
    from tesseract.config import runtime_limits

    monkeypatch.setattr(runtime_limits, "load_ask_park_timeout_s", lambda p: 0.05)

    daemon, _token = running_daemon
    sid = daemon._registry.create_session(  # noqa: SLF001
        mode="autonomy", origin="autonomy", controller_id=CONTROLLER_ID
    ).session_id
    approved = await daemon.request_permission(
        sid,
        tool="bash",
        summary="rm -rf /",
        tool_use_id="tu-headless-timeout",
        timeout_seconds=1.0,
    )
    assert approved is False
    assert daemon._parked_asks == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_invalid_message_returns_error_but_keeps_session(
    running_daemon,
) -> None:
    daemon, token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(writer, {"msg": "this-is-not-a-real-message"})
    reply = await _recv(reader)
    assert reply["event"] == "error"
    assert reply["code"] == "invalid_message"
    # Subsequent valid traffic still works.
    await _send(writer, {"msg": "list_sessions"})
    reply2 = await _recv(reader)
    assert reply2["event"] == "session_list"
    writer.close()
    await writer.wait_closed()
