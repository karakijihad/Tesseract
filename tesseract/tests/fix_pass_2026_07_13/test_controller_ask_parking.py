"""Controller-daemon ASK parking (Option B, 2026-07-13).

`ControllerDaemon.request_permission` no longer denies immediately when no
interactive client is attached, nor when an attached client stays silent
past its `timeout_seconds` window — both fall through to PARKING: the SAME
future stays daemon-owned (never crosses processes), Mirror only ever sees
a view (`ControllerAskParkedPush` / `parked_asks_snapshot`) and a
`decide_parked_ask` verb that resolves it. The park window itself
(`runtime.yaml::ask_park_timeout_s`) is the new no-forever-hang bound.
"""

from __future__ import annotations

import asyncio

import pytest

from tesseract.config import runtime_limits
from tesseract.kernel.sandbox._ipc_frames import decode_frame, encode_frame
from tesseract.orchestrator.tars_controller import (
    ControllerDaemon,
    SessionRegistry,
    TranscriptReader,
    auth,
)

CONTROLLER_ID = "ctrl-test-park-0001"


async def _open_client(daemon: ControllerDaemon):
    host, port = daemon.address
    return await asyncio.open_connection(host, port)


async def _send(writer: asyncio.StreamWriter, payload: dict) -> None:
    writer.write(encode_frame(payload))
    await writer.drain()


async def _recv(reader: asyncio.StreamReader, *, timeout: float = 5.0):
    try:
        return await asyncio.wait_for(decode_frame(reader), timeout=timeout)
    except asyncio.IncompleteReadError:
        return None


async def _recv_until(reader: asyncio.StreamReader, event: str, *, timeout: float = 5.0):
    """Drain pushes (activity_event chatter etc.) until `event` arrives."""
    async def _inner():
        while True:
            msg = await _recv(reader, timeout=timeout)
            assert msg is not None
            if msg.get("event") == event:
                return msg
    return await asyncio.wait_for(_inner(), timeout=timeout)


@pytest.fixture
async def running_daemon(isolated_home):
    token = auth.mint_token()
    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    yield daemon, token
    await daemon.stop()


def _fast_park(monkeypatch, timeout: float = 5.0) -> None:
    monkeypatch.setattr(runtime_limits, "load_ask_park_timeout_s", lambda p: timeout)


# ── park-on-unattached (no immediate deny) ──────────────────────────────────


async def test_unattached_session_parks_not_denies(running_daemon, monkeypatch) -> None:
    _fast_park(monkeypatch, timeout=5.0)
    daemon, _token = running_daemon
    sid = daemon._registry.create_session(  # noqa: SLF001
        mode="autonomy", origin="autonomy", controller_id=CONTROLLER_ID
    ).session_id

    task = asyncio.create_task(
        daemon.request_permission(
            sid, tool="bash", summary="rm -rf /", tool_use_id="tu-1", timeout_seconds=1.0
        )
    )
    for _ in range(200):
        if any(e.tool_use_id == "tu-1" for e in daemon._parked_asks.values()):  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("headless ask never parked — must not deny immediately")

    # Not yet resolved — proves it's genuinely waiting, not pre-decided.
    assert not task.done()

    entry = next(e for e in daemon._parked_asks.values() if e.tool_use_id == "tu-1")  # noqa: SLF001
    entry.future.set_result(True)
    assert await task is True


# ── decide-settles-future (IPC verb) ────────────────────────────────────────


async def test_decide_parked_ask_settles_future(running_daemon, monkeypatch) -> None:
    _fast_park(monkeypatch, timeout=5.0)
    daemon, token = running_daemon
    sid = daemon._registry.create_session(  # noqa: SLF001
        mode="autonomy", origin="autonomy", controller_id=CONTROLLER_ID
    ).session_id

    task = asyncio.create_task(
        daemon.request_permission(
            sid, tool="bash", summary="ls", tool_use_id="tu-2", timeout_seconds=0.5
        )
    )
    for _ in range(200):
        if any(e.tool_use_id == "tu-2" for e in daemon._parked_asks.values()):  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("ask never parked")
    entry = next(e for e in daemon._parked_asks.values() if e.tool_use_id == "tu-2")  # noqa: SLF001

    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(
        writer,
        {
            "msg": "decide_parked_ask",
            "approval_id": entry.approval_id,
            "approved": True,
            "operator_note": None,
        },
    )
    ack = await _recv_until(reader, "ack")
    assert ack["msg"] == "decide_parked_ask"
    assert await task is True
    assert entry.approval_id not in daemon._parked_asks  # noqa: SLF001
    writer.close()
    await writer.wait_closed()


async def test_decide_parked_ask_unknown_id_errors(running_daemon) -> None:
    daemon, token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(
        writer,
        {
            "msg": "decide_parked_ask",
            "approval_id": "does-not-exist",
            "approved": True,
            "operator_note": None,
        },
    )
    reply = await _recv_until(reader, "error")
    assert reply["code"] == "unknown_or_settled_parked_ask"
    writer.close()
    await writer.wait_closed()


async def test_decide_parked_ask_requires_no_attach(running_daemon, monkeypatch) -> None:
    """The whole point of Option B: a client that never attached to the
    session (unlike `_on_approval`'s requirement) can still settle a
    parked ask — that's WHY it parked in the first place."""
    _fast_park(monkeypatch, timeout=5.0)
    daemon, token = running_daemon
    sid = daemon._registry.create_session(  # noqa: SLF001
        mode="autonomy", origin="autonomy", controller_id=CONTROLLER_ID
    ).session_id

    task = asyncio.create_task(
        daemon.request_permission(
            sid, tool="bash", summary="ls", tool_use_id="tu-3", timeout_seconds=0.2
        )
    )
    for _ in range(200):
        if any(e.tool_use_id == "tu-3" for e in daemon._parked_asks.values()):  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("ask never parked")
    entry = next(e for e in daemon._parked_asks.values() if e.tool_use_id == "tu-3")  # noqa: SLF001

    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    # Deliberately no `attach` message — conn.sessions is empty.
    await _send(
        writer,
        {"msg": "decide_parked_ask", "approval_id": entry.approval_id, "approved": False},
    )
    ack = await _recv_until(reader, "ack")
    assert ack["msg"] == "decide_parked_ask"
    assert await task is False
    writer.close()
    await writer.wait_closed()


# ── park timeout → deny ──────────────────────────────────────────────────────


async def test_park_timeout_denies(running_daemon, monkeypatch) -> None:
    _fast_park(monkeypatch, timeout=0.05)
    daemon, _token = running_daemon
    sid = daemon._registry.create_session(  # noqa: SLF001
        mode="autonomy", origin="autonomy", controller_id=CONTROLLER_ID
    ).session_id

    approved = await daemon.request_permission(
        sid, tool="bash", summary="ls", tool_use_id="tu-4", timeout_seconds=0.05
    )
    assert approved is False
    assert daemon._parked_asks == {}  # noqa: SLF001

    events = [event for event, _ in TranscriptReader(sid).read_from()]
    dumped = [e.model_dump(mode="json") for e in events]
    resolutions = [(e.get("resolution"), e["resolved"]) for e in dumped]
    assert (None, False) in resolutions
    assert ("parked", False) in resolutions
    assert ("park_timeout", True) in resolutions


async def test_attached_silent_timeout_then_parks(running_daemon, monkeypatch) -> None:
    """Attached-but-silent: the interactive wait expires, but instead of
    denying, the ask parks and is later settled — proving the SAME future
    survives past the first (unshielded-looking) timeout."""
    _fast_park(monkeypatch, timeout=5.0)
    daemon, token = running_daemon
    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(
        writer, {"msg": "new_session", "title": None, "mode": "chat", "origin": "cli"}
    )
    attached = await _recv_until(reader, "attached")
    sid = attached["session"]["session_id"]

    task = asyncio.create_task(
        daemon.request_permission(
            sid, tool="bash", summary="ls", tool_use_id="tu-5", timeout_seconds=0.05
        )
    )
    for _ in range(200):
        if any(e.tool_use_id == "tu-5" for e in daemon._parked_asks.values()):  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("attached-but-silent ask never parked")

    entry = next(e for e in daemon._parked_asks.values() if e.tool_use_id == "tu-5")  # noqa: SLF001
    entry.future.set_result(True)
    assert await task is True
    writer.close()
    await writer.wait_closed()


# ── snapshot verb ────────────────────────────────────────────────────────────


async def test_parked_asks_snapshot_lists_current(running_daemon, monkeypatch) -> None:
    _fast_park(monkeypatch, timeout=5.0)
    daemon, token = running_daemon
    sid = daemon._registry.create_session(  # noqa: SLF001
        mode="autonomy", origin="autonomy", controller_id=CONTROLLER_ID
    ).session_id

    task = asyncio.create_task(
        daemon.request_permission(
            sid, tool="bash", summary="ls -la", tool_use_id="tu-6", timeout_seconds=0.1
        )
    )
    for _ in range(200):
        if any(e.tool_use_id == "tu-6" for e in daemon._parked_asks.values()):  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("ask never parked")

    reader, writer = await _open_client(daemon)
    await _send(writer, {"auth": token})
    await _send(writer, {"msg": "parked_asks_snapshot"})
    snap = await _recv_until(reader, "parked_asks_snapshot")
    tool_use_ids = {item["tool_use_id"] for item in snap["items"]}
    assert "tu-6" in tool_use_ids
    item = next(i for i in snap["items"] if i["tool_use_id"] == "tu-6")
    assert item["tool"] == "bash"
    assert item["summary"] == "ls -la"

    # Clean up: settle so the daemon.stop() drain in the fixture has nothing
    # left hanging past the test.
    entry = next(e for e in daemon._parked_asks.values() if e.tool_use_id == "tu-6")  # noqa: SLF001
    entry.future.set_result(False)
    assert await task is False
    writer.close()
    await writer.wait_closed()


# ── drain-on-stop ────────────────────────────────────────────────────────────


async def test_stop_drains_parked_asks(running_daemon, monkeypatch) -> None:
    _fast_park(monkeypatch, timeout=9999.0)  # long enough that only stop() can resolve it
    daemon, _token = running_daemon
    sid = daemon._registry.create_session(  # noqa: SLF001
        mode="autonomy", origin="autonomy", controller_id=CONTROLLER_ID
    ).session_id

    task = asyncio.create_task(
        daemon.request_permission(
            sid, tool="bash", summary="ls", tool_use_id="tu-7", timeout_seconds=0.05
        )
    )
    for _ in range(200):
        if any(e.tool_use_id == "tu-7" for e in daemon._parked_asks.values()):  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("ask never parked")

    await daemon.stop()
    assert daemon._parked_asks == {}  # noqa: SLF001
    with pytest.raises(asyncio.CancelledError):
        await task
