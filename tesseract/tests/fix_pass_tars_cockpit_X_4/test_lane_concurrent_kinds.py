"""X-4 Session B — two concurrent lanes (claude + codex) run in
parallel without cross-contamination.

The contract: per-lane is serial; across-lane is parallel. This test
opens both kinds and runs sends concurrently via `asyncio.gather`,
then asserts each lane's events.jsonl only contains events for that
lane (lane_id discriminator at every event)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from tesseract.orchestrator.tars_controller.lanes import (
    Lane,
    LaneManager,
)
from tesseract.orchestrator.tars_controller.lanes.manager import LaneRuntime


class _SlowClaudeAdapter:
    """Sleeps for 50ms between events to maximize the window for any
    cross-lane interleaving bug to manifest."""

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        on_event({"type": "system", "subtype": "init", "session_id": "claude-sess"})
        await asyncio.sleep(0.05)
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"claude: {message}"}]},
        })
        await asyncio.sleep(0.05)
        on_event({"type": "result", "subtype": "success", "result": "", "usage": {}})
        return {"session_id": "claude-sess", "is_error": False, "usage": {}}


class _SlowCodexAdapter:
    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        on_event({"type": "thread.started", "thread_id": "codex-thread"})
        await asyncio.sleep(0.05)
        on_event({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": f"codex: {message}"},
        })
        await asyncio.sleep(0.05)
        on_event({"type": "turn.completed", "usage": {}})
        return {"session_id": "codex-thread", "is_error": False, "usage": {}}


def _kind_factory(lane: Lane, runtime: LaneRuntime) -> Any:
    if lane.kind == "claude":
        return _SlowClaudeAdapter()
    return _SlowCodexAdapter()


def test_two_lanes_run_concurrently_without_interference(
    isolated_home: Path,
) -> None:
    mgr = LaneManager(adapter_factory=_kind_factory)

    async def _scenario() -> tuple[str, str]:
        claude_id, codex_id = await asyncio.gather(
            mgr.open(
                kind="claude",
                mode="headless",
                model="claude-sonnet-4-6",
                working_dir=str(isolated_home),
            ),
            mgr.open(
                kind="codex",
                mode="headless",
                model="gpt-5",
                working_dir=str(isolated_home),
            ),
        )
        await asyncio.gather(
            mgr.send(claude_id, "do thing A"),
            mgr.send(codex_id, "do thing B"),
        )
        # send is fire-and-queue — settle both turns before the loop closes.
        await asyncio.gather(mgr.drain(claude_id), mgr.drain(codex_id))
        return claude_id, codex_id

    claude_id, codex_id = asyncio.run(_scenario())

    claude_events, _ = mgr.read(claude_id, None)
    codex_events, _ = mgr.read(codex_id, None)

    assert all(e.lane_id == claude_id for e in claude_events)
    assert all(e.lane_id == codex_id for e in codex_events)

    claude_texts = [
        e.payload.get("text") for e in claude_events if e.kind == "assistant_text"
    ]
    codex_texts = [
        e.payload.get("text") for e in codex_events if e.kind == "assistant_text"
    ]
    assert any("claude:" in (t or "") for t in claude_texts)
    assert any("codex:" in (t or "") for t in codex_texts)


def test_per_lane_serial_within_concurrent_lanes(isolated_home: Path) -> None:
    """Per-lane FIFO holds even while two lanes run in parallel.
    Sending three messages serially to one lane while another lane is
    busy must complete in order — turn_ended events appear in the order
    the messages were issued."""
    mgr = LaneManager(adapter_factory=_kind_factory)

    async def _scenario() -> str:
        lane_id = await mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
        # Codex lane runs in parallel to give the serial-Claude path real
        # contention from cross-lane scheduling.
        codex_id = await mgr.open(
            kind="codex",
            mode="headless",
            model="gpt-5",
            working_dir=str(isolated_home),
        )
        await asyncio.gather(
            mgr.send(lane_id, "first"),
            mgr.send(lane_id, "second"),
            mgr.send(lane_id, "third"),
            mgr.send(codex_id, "parallel"),
        )
        # send is fire-and-queue — settle all queued turns before the
        # loop closes (per-lane FIFO is preserved by task/lock order).
        await asyncio.gather(mgr.drain(lane_id), mgr.drain(codex_id))
        return lane_id

    lane_id = asyncio.run(_scenario())

    events, _ = mgr.read(lane_id, None)
    turn_started_messages = [
        e.payload.get("message") for e in events if e.kind == "turn_started"
    ]
    assert turn_started_messages == ["first", "second", "third"]
