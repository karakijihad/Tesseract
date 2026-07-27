"""X-4 Session C — true brain-restart invariant over the wire.

The contract: when TARS brain restarts, the controller daemon stays
alive (separate process; the supervisor owns its lifecycle) and every
lane survives. The new brain re-establishes visibility via
`lane.attach(lane_id)` over IPC.

In tests, the "brain" is a `ControllerClient` connection. "Restart"
means closing that client and connecting a fresh one to the same
daemon. The daemon — and therefore every lane — survives across the
client churn. This is the canonical P-3 wire-level assertion.

Spawning the daemon as a true OS subprocess via the entry-point script
would prove process-level isolation too, but the load-bearing claim
('brain lifecycle is decoupled from controller / lane lifecycle') is
already proven when the daemon runs in the same process — what
matters is that BRAIN STATE (the ControllerClient + any in-memory
runtime) is fully disposable while the controller-owned LaneManager
keeps lanes alive. The Session B in-process simulation covers the
"new LaneManager attaches to disk-persisted state" angle; this test
covers the IPC angle that Session B couldn't reach."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest

from tesseract.orchestrator.tars_controller import (
    ControllerClient,
    ControllerDaemon,
    SessionRegistry,
)
from tesseract.orchestrator.tars_controller.lanes import Lane, LaneManager
from tesseract.orchestrator.tars_controller.lanes.manager import LaneRuntime


class _StubAdapter:
    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        on_event({
            "type": "system",
            "subtype": "init",
            "session_id": "brain-restart-sess",
        })
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"reply: {message}"}]},
        })
        on_event({"type": "result", "subtype": "success", "result": "", "usage": {}})
        return {
            "session_id": "brain-restart-sess",
            "is_error": False,
            "usage": {},
        }


def _stub_factory(lane: Lane, runtime: LaneRuntime) -> Any:
    return _StubAdapter()


async def test_attach_over_ipc_after_client_restart(
    isolated_home: Path,
) -> None:
    """The P-3 wire-level proof.

    1. Boot daemon + lane_manager.
    2. Client A connects, opens a lane, sends a turn, reads events.
    3. Client A closes (simulating brain shutdown).
    4. Client B connects to the SAME daemon (daemon stays alive).
    5. Client B calls `lane_attach(lane_id)`; verifies snapshot.
    6. Client B sends another turn; verifies cursor advances.
    """
    token = "restart-test-" + "0" * 20
    daemon = ControllerDaemon(
        controller_id="ctrl-restart-test",
        token=token,
        registry=SessionRegistry(),
        lane_manager=LaneManager(adapter_factory=_stub_factory),
        heartbeat_interval=60.0,
    )
    await daemon.start(host="127.0.0.1", port=0)
    host, port = daemon.address

    try:
        # ---- Client A (pre-restart brain) -------------------------------
        client_a = await ControllerClient.connect(
            host=host, port=port, token=token, connect_timeout=2.0
        )
        lane_id = await client_a.lane_open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
        await client_a.lane_send(lane_id, "pre-restart turn")
        pre_read = await client_a.lane_read(lane_id, since_cursor=None)
        pre_cursor = pre_read["next_cursor"]
        pre_event_count = pre_read["count"]
        assert pre_event_count >= 4
        assert int(pre_cursor) > 0

        # Brain shutdown — client A goes away. The daemon stays alive.
        await client_a.close()

        # ---- Client B (post-restart brain) ------------------------------
        client_b = await ControllerClient.connect(
            host=host, port=port, token=token, connect_timeout=2.0
        )
        # Brain discovers lanes via list_ids.
        ids = await client_b.lane_list()
        assert lane_id in ids

        # Attach pulls the snapshot — the recovery primitive.
        snapshot = await client_b.lane_attach(lane_id)
        assert snapshot["lane"]["lane_id"] == lane_id
        assert snapshot["lane"]["cli_session_id"] == "brain-restart-sess"
        assert len(snapshot["recent_events"]) == pre_event_count
        assert snapshot["next_cursor"] == pre_cursor

        # The recovered lane is fully drivable from the new client.
        await client_b.lane_send(lane_id, "post-restart turn")
        post_read = await client_b.lane_read(lane_id, since_cursor=pre_cursor)
        assert post_read["count"] >= 3  # turn_started + assistant_text + turn_ended
        assert int(post_read["next_cursor"]) > int(pre_cursor)

        await client_b.close()
    finally:
        await daemon.stop()


async def test_multiple_clients_can_attach_concurrently(
    isolated_home: Path,
) -> None:
    """A lane can be observed by multiple brain clients simultaneously.
    Demonstrates that `attach` is read-side (no exclusive ownership)."""
    token = "multi-attach-" + "0" * 20
    daemon = ControllerDaemon(
        controller_id="ctrl-multi-attach",
        token=token,
        registry=SessionRegistry(),
        lane_manager=LaneManager(adapter_factory=_stub_factory),
        heartbeat_interval=60.0,
    )
    await daemon.start(host="127.0.0.1", port=0)
    host, port = daemon.address
    try:
        opener = await ControllerClient.connect(
            host=host, port=port, token=token, connect_timeout=2.0
        )
        lane_id = await opener.lane_open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
        await opener.lane_send(lane_id, "shared lane turn")
        await opener.close()

        async def _attach_and_read(token: str) -> dict[str, Any]:
            c = await ControllerClient.connect(
                host=host, port=port, token=token, connect_timeout=2.0
            )
            try:
                snap = await c.lane_attach(lane_id)
                return snap
            finally:
                await c.close()

        results = await asyncio.gather(
            _attach_and_read(token),
            _attach_and_read(token),
            _attach_and_read(token),
        )
        for snap in results:
            assert snap["lane"]["lane_id"] == lane_id
            assert len(snap["recent_events"]) >= 4
    finally:
        await daemon.stop()
