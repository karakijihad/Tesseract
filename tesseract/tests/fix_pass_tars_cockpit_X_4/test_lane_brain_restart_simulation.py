"""X-4 Session B — brain-restart recovery invariant (in-process).

The P-3 invariant: when the TARS brain restarts, the controller daemon
+ all lanes survive (separate processes), and the brain re-establishes
visibility via `lane.attach(lane_id)`. We simulate the brain-side
restart in-process by:

1. Building a `LaneManager`, opening a lane, sending a turn.
2. Discarding that manager (simulating brain death — its in-memory
   runtime cache evaporates).
3. Building a fresh `LaneManager` against the same on-disk state.
4. Listing lane ids from disk (the brain's discovery path).
5. Calling `attach(lane_id)` and verifying the snapshot includes the
   full prior history + a correct `next_cursor`.

A true subprocess test (kill the brain OS process) lands in Session
C where the daemon IPC handlers expose `lane.attach` over the wire."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from tesseract.orchestrator.tars_controller.lanes import (
    Lane,
    LaneManager,
)
from tesseract.orchestrator.tars_controller.lanes.manager import LaneRuntime


class _StubClaudeAdapter:
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
            "session_id": "sess-restart-test",
        })
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "before restart"}]},
        })
        on_event({"type": "result", "subtype": "success", "result": "", "usage": {}})
        return {"session_id": "sess-restart-test", "is_error": False, "usage": {}}


def _stub_factory(lane: Lane, runtime: LaneRuntime) -> Any:
    return _StubClaudeAdapter()


def test_attach_recovers_full_history_after_brain_restart(
    isolated_home: Path,
) -> None:
    """Build → send → drop manager → fresh manager → attach. The
    snapshot's `recent_events` MUST contain everything the old manager
    wrote, and `next_cursor` MUST equal the file size on disk."""
    mgr_a = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr_a.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(mgr_a.send(lane_id, "first turn"))
    pre_events, pre_cursor = mgr_a.read(lane_id, None)
    assert len(pre_events) >= 4  # status_change + turn_started + assistant_text + turn_ended
    del mgr_a

    mgr_b = LaneManager(adapter_factory=_stub_factory)
    assert lane_id in mgr_b.list_ids()

    snapshot = asyncio.run(mgr_b.attach(lane_id))
    assert snapshot.lane.lane_id == lane_id
    assert snapshot.lane.cli_session_id == "sess-restart-test"
    assert len(snapshot.recent_events) == len(pre_events)
    assert snapshot.next_cursor == pre_cursor
    assert lane_id in mgr_b._runtimes


def test_attach_keeps_lifecycle_from_disk_after_restart(
    isolated_home: Path,
) -> None:
    """After restart, attach reads the persisted lifecycle from
    `lane.json` rather than defaulting to `spawning`. Pre-restart the
    lane was `ready`; the new manager must see `ready`, not `spawning`."""
    mgr_a = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr_a.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    del mgr_a

    mgr_b = LaneManager(adapter_factory=_stub_factory)
    snapshot = asyncio.run(mgr_b.attach(lane_id))
    assert snapshot.lane.lifecycle == "ready"


def test_next_cursor_advances_after_post_attach_send(
    isolated_home: Path,
) -> None:
    """A post-restart `send` must extend events.jsonl past the
    pre-restart cursor — proves the recovered runtime can still drive
    new turns against the on-disk session."""
    mgr_a = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr_a.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(mgr_a.send(lane_id, "before"))
    _, pre_cursor = mgr_a.read(lane_id, None)
    del mgr_a

    mgr_b = LaneManager(adapter_factory=_stub_factory)
    asyncio.run(mgr_b.attach(lane_id))
    asyncio.run(mgr_b.send(lane_id, "after"))
    post_events, post_cursor = mgr_b.read(lane_id, pre_cursor)
    assert len(post_events) >= 3
    assert int(post_cursor) > int(pre_cursor)
