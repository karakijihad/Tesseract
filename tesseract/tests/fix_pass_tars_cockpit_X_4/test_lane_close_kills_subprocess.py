"""X-4 Session D — `lane_close` must terminate the underlying CLI.

For headless lanes: the adapter has no `close()` method (each turn is
a short-lived per-turn subprocess); `lane_close` only marks state +
archives. A mid-turn close fires the runtime's `cancel_event` which
the headless adapter's per-turn loop honors (already in `cli_adapter.py`).

P4 prune (2026-07-04): the PTY-lane close-grace/force-kill tests moved
with `_PtyLaneAdapter` when the `pty` lane mode was retired."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from tesseract.orchestrator.tars_controller.lanes import Lane, LaneManager
from tesseract.orchestrator.tars_controller.lanes.manager import LaneRuntime


async def test_headless_close_marks_state_without_terminate(
    isolated_home: Path,
) -> None:
    """Headless adapters have no `close()` method — `lane_close` is a
    no-op at the subprocess layer (each turn is a fresh subprocess
    already gone by close time). State marking + archive must still
    fire."""

    class _StubHeadless:
        async def run_turn(
            self,
            *,
            message: str,
            on_event: Callable[[dict[str, Any]], None],
            cancel_event: asyncio.Event | None,
        ) -> dict[str, Any]:
            return {"session_id": None, "is_error": False, "usage": {}}

    def _h(lane: Lane, runtime: LaneRuntime) -> Any:
        return _StubHeadless()

    mgr = LaneManager(adapter_factory=_h)
    lane_id = await mgr.open(
        kind="claude",
        mode="headless",
        model="claude-sonnet-4-6",
        working_dir=str(isolated_home),
    )
    await mgr.send(lane_id, "hi")
    result = await mgr.close(lane_id, reason="operator_close")
    assert result["final_status"] == "closed"
