"""X-4 Session C — daemon-side IPC handlers for `lane.*`.

Each test boots a real ``ControllerDaemon`` over loopback TCP, connects
a real ``ControllerClient``, and round-trips the seven `lane_*`
methods. The daemon owns a `LaneManager` built with a stub adapter so
no live Claude / Codex CLI subprocess is spawned.

This is the "external brain drives lanes" path — proves Mirror (or
any out-of-process brain) can talk to the controller's lane substrate
without hosting its own LaneManager."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest

from tesseract.orchestrator.tars_controller import (
    ControllerClient,
    ControllerClientError,
    ControllerDaemon,
    SessionRegistry,
)
from tesseract.orchestrator.tars_controller.lanes import (
    Lane,
    LaneManager,
)
from tesseract.orchestrator.tars_controller.lanes.manager import LaneRuntime


class _StubAdapter:
    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        on_event({"type": "system", "subtype": "init", "session_id": "ipc-sess"})
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"ipc reply: {message}"}]},
        })
        on_event({"type": "result", "subtype": "success", "result": "", "usage": {}})
        return {"session_id": "ipc-sess", "is_error": False, "usage": {}}


def _stub_factory(lane: Lane, runtime: LaneRuntime) -> Any:
    return _StubAdapter()


async def _await_turn_ended(
    client: ControllerClient, lane_id: str, timeout_s: float = 5.0
) -> None:
    """lane_send acks 'queued' (fire-and-queue); completion is the
    turn_ended event. Poll lane_read over the wire — the production
    wait contract — until it lands."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    cursor: str | None = None
    while asyncio.get_running_loop().time() < deadline:
        result = await client.lane_read(lane_id, since_cursor=cursor)
        if any(e["kind"] == "turn_ended" for e in result["events"]):
            return
        cursor = result["next_cursor"]
        await asyncio.sleep(0.01)
    raise AssertionError(f"lane {lane_id}: no turn_ended within {timeout_s}s")


@pytest.fixture
async def daemon_client(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """Boot a real ControllerDaemon + ControllerClient for one test.

    The daemon binds to 127.0.0.1:0 (kernel-assigned port); the client
    connects with explicit host/port/token — no port-file resolution."""
    token = "x4c-test-token-" + "0" * 16
    daemon = ControllerDaemon(
        controller_id="ctrl-x4c-test",
        token=token,
        registry=SessionRegistry(),
        lane_manager=LaneManager(adapter_factory=_stub_factory),
        heartbeat_interval=60.0,
    )
    await daemon.start(host="127.0.0.1", port=0)
    host, port = daemon.address

    client = await ControllerClient.connect(
        host=host, port=port, token=token, connect_timeout=2.0
    )
    try:
        yield client, daemon
    finally:
        await client.close()
        await daemon.stop()


@pytest.mark.asyncio
async def test_lane_open_and_list_round_trip(
    daemon_client: tuple[ControllerClient, ControllerDaemon],
    isolated_home: Path,
) -> None:
    client, _ = daemon_client
    lane_id = await client.lane_open(
        kind="claude",
        mode="headless",
        model="claude-sonnet-4-6",
        working_dir=str(isolated_home),
    )
    assert lane_id.startswith("lane-claude-")
    ids = await client.lane_list()
    assert lane_id in ids


@pytest.mark.asyncio
async def test_lane_send_then_read_returns_events(
    daemon_client: tuple[ControllerClient, ControllerDaemon],
    isolated_home: Path,
) -> None:
    client, _ = daemon_client
    lane_id = await client.lane_open(
        kind="claude",
        mode="headless",
        model="claude-sonnet-4-6",
        working_dir=str(isolated_home),
    )
    send_result = await client.lane_send(lane_id, "hello over the wire")
    assert send_result["accepted"] is True

    await _await_turn_ended(client, lane_id)
    read_result = await client.lane_read(lane_id, since_cursor=None)
    assert read_result["count"] >= 4
    kinds = [e["kind"] for e in read_result["events"]]
    assert "assistant_text" in kinds
    assert "turn_started" in kinds
    assert "turn_ended" in kinds


@pytest.mark.asyncio
async def test_lane_status_over_ipc(
    daemon_client: tuple[ControllerClient, ControllerDaemon],
    isolated_home: Path,
) -> None:
    client, _ = daemon_client
    lane_id = await client.lane_open(
        kind="claude",
        mode="headless",
        model="claude-sonnet-4-6",
        working_dir=str(isolated_home),
    )
    await client.lane_send(lane_id, "probe me")
    await _await_turn_ended(client, lane_id)
    status = await client.lane_status(lane_id)
    assert status["alive"] is True
    assert status["busy"] is False
    assert status["lifecycle"] == "ready"


@pytest.mark.asyncio
async def test_lane_attach_returns_snapshot_over_ipc(
    daemon_client: tuple[ControllerClient, ControllerDaemon],
    isolated_home: Path,
) -> None:
    client, _ = daemon_client
    lane_id = await client.lane_open(
        kind="claude",
        mode="headless",
        model="claude-sonnet-4-6",
        working_dir=str(isolated_home),
    )
    await client.lane_send(lane_id, "first send")
    await _await_turn_ended(client, lane_id)
    snapshot = await client.lane_attach(lane_id)
    assert snapshot["lane"]["lane_id"] == lane_id
    assert len(snapshot["recent_events"]) >= 4
    assert int(snapshot["next_cursor"]) > 0


@pytest.mark.asyncio
async def test_lane_close_archives_over_ipc(
    daemon_client: tuple[ControllerClient, ControllerDaemon],
    isolated_home: Path,
) -> None:
    client, _ = daemon_client
    lane_id = await client.lane_open(
        kind="claude",
        mode="headless",
        model="claude-sonnet-4-6",
        working_dir=str(isolated_home),
    )
    result = await client.lane_close(lane_id, reason="mission_complete")
    assert result["final_status"] == "closed"
    assert "archive_dir" in result
    # After close, the lane is no longer in the live list.
    ids = await client.lane_list()
    assert lane_id not in ids


@pytest.mark.asyncio
async def test_lane_interrupt_round_trip_over_ipc(
    daemon_client: tuple[ControllerClient, ControllerDaemon],
    isolated_home: Path,
) -> None:
    """M2: the lane_interrupt verb round-trips over IPC. An idle lane returns
    interrupted=False (the stub turn completes instantly), proving the wire
    path (message parse → handler → manager.interrupt → result) is wired."""
    client, _ = daemon_client
    lane_id = await client.lane_open(
        kind="claude",
        mode="headless",
        model="claude-sonnet-4-6",
        working_dir=str(isolated_home),
    )
    result = await client.lane_interrupt(lane_id)
    assert result["interrupted"] is False


@pytest.mark.asyncio
async def test_unknown_lane_id_attach_raises_client_error(
    daemon_client: tuple[ControllerClient, ControllerDaemon],
) -> None:
    client, _ = daemon_client
    with pytest.raises(ControllerClientError) as excinfo:
        await client.lane_attach("lane-claude-doesnotexist")
    assert "lane_attach" in str(excinfo.value)


@pytest.mark.asyncio
async def test_concurrent_lane_calls_demultiplex_by_request_id(
    daemon_client: tuple[ControllerClient, ControllerDaemon],
    isolated_home: Path,
) -> None:
    """Two concurrent `lane_open` calls return distinct lane ids.
    If the client's request_id demux is broken, one call could
    receive the other's reply — surfacing as a shared lane id."""
    client, _ = daemon_client
    a, b = await asyncio.gather(
        client.lane_open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        ),
        client.lane_open(
            kind="codex",
            mode="headless",
            model="gpt-5",
            working_dir=str(isolated_home),
        ),
    )
    assert a != b
    assert a.startswith("lane-claude-")
    assert b.startswith("lane-codex-")


@pytest.mark.asyncio
async def test_lane_call_cancellation_does_not_leak_waiter(
    daemon_client: tuple[ControllerClient, ControllerDaemon],
    isolated_home: Path,
) -> None:
    """Reviewer Important #1 regression guard — `asyncio.CancelledError`
    raised in the awaiting task MUST clean up the `_lane_request_waiters`
    entry. Previously `except Exception` missed CancelledError (BaseException
    in 3.12); `finally` now catches all exit paths."""
    client, _ = daemon_client
    # Issue a lane_call with an absurd timeout, then cancel the awaiting
    # task immediately. The waiter dict must be empty after cleanup.
    task = asyncio.create_task(
        client.lane_open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    await asyncio.sleep(0)  # let the task register its waiter
    task.cancel()
    with pytest.raises((asyncio.CancelledError, ControllerClientError)):
        await task
    # The cleanup path must have popped the waiter — no stale entries.
    assert client._lane_request_waiters == {}


async def test_lane_manager_unwired_returns_clean_error(
    isolated_home: Path,
) -> None:
    """A daemon built without a lane_manager must surface every lane.*
    request as `ok=False, error="lane_manager_unwired"` — the client
    raises ControllerClientError so callers degrade cleanly."""
    token = "unwired-token-" + "0" * 16
    daemon = ControllerDaemon(
        controller_id="ctrl-x4c-unwired",
        token=token,
        registry=SessionRegistry(),
        lane_manager=None,
        heartbeat_interval=60.0,
    )
    await daemon.start(host="127.0.0.1", port=0)
    host, port = daemon.address
    client = await ControllerClient.connect(
        host=host, port=port, token=token, connect_timeout=2.0
    )
    try:
        with pytest.raises(ControllerClientError) as excinfo:
            await client.lane_list()
        assert "lane_manager_unwired" in str(excinfo.value)
    finally:
        await client.close()
        await daemon.stop()
