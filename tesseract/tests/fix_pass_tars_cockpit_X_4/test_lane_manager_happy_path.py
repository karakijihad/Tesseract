"""X-4 Session A — full six-method round-trip via a stub adapter.

A real `claude` / `codex` invocation is out of scope for a unit test;
the stub adapter emits canonical stream-JSON events for both kinds so
the lane translator (`_translate_adapter_event`) gets full coverage on
the substrate side. Brain-restart subprocess test ships in Session B.

Pins:
- `LaneEventKind.assistant_text` and `tool_result` are emitted as
  distinct events (audit-2026-05-24 Critical regression guard).
- `cli_session_id` threads from the system-init event into `lane.json`
  so a future turn would route through `--resume <id>`.
- `close` archives the lane dir + emits a `closed` event before the
  archive move (so the event is part of the moved payload).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest

from tesseract.orchestrator.tars_controller.lanes import (
    Lane,
    LaneManager,
    read_lane,
)
from tesseract.orchestrator.tars_controller.lanes.manager import LaneRuntime


class _StubClaudeAdapter:
    """Emits a canonical Claude stream-json turn:

    system/init → assistant(text) → user(tool_result) → result(success).
    The lane translator should produce four distinct LaneEvent kinds:
    status_change (from system/init), assistant_text, tool_result, and
    the turn-boundary `turn_ended` (stamped by the manager itself, not
    the translator)."""

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        on_event({"type": "system", "subtype": "init", "session_id": "sess-xyz"})
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "stub reply"}]},
        })
        on_event({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-1",
                    "content": "ok",
                    "is_error": False,
                }],
            },
        })
        on_event({"type": "result", "subtype": "success", "result": "stub reply", "usage": {}})
        return {"session_id": "sess-xyz", "is_error": False, "usage": {}}


class _StubCodexAdapter:
    """Emits a canonical Codex stream-json turn:

    thread.started → item.completed(agent_message) → turn.completed."""

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        on_event({"type": "thread.started", "thread_id": "thread-abc"})
        on_event({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "stub codex reply"},
        })
        on_event({"type": "turn.completed", "usage": {"input_tokens": 10}})
        return {"session_id": "thread-abc", "is_error": False, "usage": {"input_tokens": 10}}


def _stub_factory(lane: Lane, runtime: LaneRuntime) -> Any:
    if lane.kind == "claude":
        return _StubClaudeAdapter()
    return _StubCodexAdapter()


async def _send_and_drain(mgr: LaneManager, lane_id: str, message: str) -> None:
    """send is fire-and-queue — the ack means 'queued'. Drain inside the
    same event loop so the turn task completes before assertions."""
    result = await mgr.send(lane_id, message)
    assert result.accepted
    await mgr.drain(lane_id)


def test_open_returns_lane_id_and_writes_lane_json(isolated_home: Path) -> None:
    mgr = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    assert lane_id.startswith("lane-claude-")
    lane = read_lane(lane_id)
    assert lane.kind == "claude"
    assert lane.mode == "headless"
    assert lane.lifecycle == "ready"


def test_send_emits_distinct_assistant_text_and_tool_result_events(
    isolated_home: Path,
) -> None:
    """Audit-2026-05-24 Critical regression guard at the substrate level."""
    mgr = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(_send_and_drain(mgr, lane_id, "do the thing"))

    events, _ = mgr.read(lane_id, None)
    kinds = [e.kind for e in events]
    # status_change (open) + turn_started + status_change (init) +
    # assistant_text + tool_result + turn_ended — order may vary slightly
    # but `assistant_text` and `tool_result` MUST both appear and be
    # distinct.
    assert "assistant_text" in kinds
    assert "tool_result" in kinds
    assert kinds.count("assistant_text") == 1
    assert kinds.count("tool_result") == 1
    assert kinds.count("turn_started") == 1
    assert kinds.count("turn_ended") == 1


def test_send_threads_cli_session_id_into_lane_json(
    isolated_home: Path,
) -> None:
    mgr = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(_send_and_drain(mgr, lane_id, "hello"))
    persisted = read_lane(lane_id)
    assert persisted.cli_session_id == "sess-xyz"


def test_status_reflects_post_turn_idle(isolated_home: Path) -> None:
    mgr = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(_send_and_drain(mgr, lane_id, "hi"))

    status = mgr.status(lane_id)
    assert status.alive is True
    assert status.busy is False
    assert status.queue_depth == 0
    assert status.current_turn_id is None
    assert status.end_of_turn_at_utc is not None


def test_attach_returns_full_history_and_next_cursor(
    isolated_home: Path,
) -> None:
    """Attach is the brain-restart recovery primitive. After two sends,
    a fresh attach reads everything from offset 0 and returns the EOF
    cursor."""
    mgr = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(_send_and_drain(mgr, lane_id, "first"))
    asyncio.run(_send_and_drain(mgr, lane_id, "second"))

    snap = asyncio.run(mgr.attach(lane_id))
    assert snap.lane.lane_id == lane_id
    assert len(snap.recent_events) >= 6  # two turns + open status_change
    assert snap.next_cursor.isdigit()
    assert int(snap.next_cursor) > 0


def test_close_archives_lane_dir(isolated_home: Path) -> None:
    mgr = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    result = asyncio.run(mgr.close(lane_id, reason="mission_complete"))

    assert result["final_status"] == "closed"
    assert "archive_dir" in result
    archive_dir = Path(result["archive_dir"])
    assert archive_dir.exists()
    assert (archive_dir / "lane.json").exists()
    # `closed` event was appended BEFORE the archive move so it's in
    # the moved events.jsonl, not the original (now gone) one.
    events_log = archive_dir / "events.jsonl"
    if events_log.exists():
        lines = events_log.read_text(encoding="utf-8").splitlines()
        assert any('"kind":"closed"' in line for line in lines)


class _StubClaudeAdapterFusedAssistant:
    """Reviewer-finding regression guard: a single Claude assistant
    message carries BOTH a text block AND a tool_use block. The
    translator MUST emit two distinct LaneEvents (one assistant_text,
    one tool_use) and drop neither."""

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        on_event({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I'll inspect the file now."},
                    {
                        "type": "tool_use",
                        "id": "tu-99",
                        "name": "Read",
                        "input": {"path": "/tmp/x"},
                    },
                ],
            },
        })
        on_event({"type": "result", "subtype": "success", "result": "", "usage": {}})
        return {"session_id": "sess-fused", "is_error": False, "usage": {}}


def test_claude_assistant_with_text_and_tool_use_emits_both(
    isolated_home: Path,
) -> None:
    """Reviewer Critical #5 fix — neither conflate nor drop. The text
    AND the tool_use must both appear as distinct LaneEvents."""

    def _factory(lane: Lane, runtime: LaneRuntime) -> Any:
        return _StubClaudeAdapterFusedAssistant()

    mgr = LaneManager(adapter_factory=_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(_send_and_drain(mgr, lane_id, "do the thing"))

    events, _ = mgr.read(lane_id, None)
    kinds = [e.kind for e in events]
    assert kinds.count("assistant_text") == 1
    assert kinds.count("tool_use") == 1
    text_events = [e for e in events if e.kind == "assistant_text"]
    tool_events = [e for e in events if e.kind == "tool_use"]
    assert "inspect the file" in text_events[0].payload["text"]
    assert tool_events[0].payload["name"] == "Read"


class _BlockThenReplyAdapter:
    """Turn 1 blocks until cancel_event fires (a long CLI turn interrupt() must
    abort); turn 2+ replies normally with a fresh event — proves the resend
    after an interrupt runs a real, non-killed turn (M2 per-turn cancel)."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            while not (cancel_event is not None and cancel_event.is_set()):
                await asyncio.sleep(0.01)
            return {"session_id": "sess-int", "is_error": True, "usage": {}}
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "corrected"}]},
        })
        on_event({"type": "result", "subtype": "success", "result": "corrected", "usage": {}})
        return {"session_id": "sess-int", "is_error": False, "usage": {}}


def test_interrupt_then_resend_runs_fresh_turn(isolated_home: Path) -> None:
    """M2: interrupt() aborts the running turn but leaves the lane alive; the
    follow-up send runs a FRESH (non-cancelled) turn to completion — the stale
    cancel event from the killed turn must not kill the resend."""
    adapter = _BlockThenReplyAdapter()

    def _factory(lane: Lane, runtime: LaneRuntime) -> Any:
        return adapter

    mgr = LaneManager(adapter_factory=_factory)

    async def _run() -> None:
        lane_id = await mgr.open(
            kind="claude", mode="headless", model="m",
            working_dir=str(isolated_home),
        )
        await mgr.send(lane_id, "long turn")
        await asyncio.wait_for(adapter.started.wait(), timeout=2.0)
        assert mgr.status(lane_id).busy is True

        assert await mgr.interrupt(lane_id) is True
        await asyncio.wait_for(mgr.drain(lane_id), timeout=2.0)
        assert mgr.status(lane_id).busy is False
        assert mgr.status(lane_id).alive is True

        # Resend runs a fresh turn to completion (not instantly killed).
        await mgr.send(lane_id, "correction")
        await asyncio.wait_for(mgr.drain(lane_id), timeout=2.0)
        events, _ = mgr.read(lane_id, None)
        texts = [e for e in events if e.kind == "assistant_text"]
        assert any("corrected" in e.payload.get("text", "") for e in texts)

    asyncio.run(_run())


def test_interrupt_idle_lane_returns_false(isolated_home: Path) -> None:
    mgr = LaneManager(adapter_factory=_stub_factory)

    async def _run() -> None:
        lane_id = await mgr.open(
            kind="claude", mode="headless", model="m",
            working_dir=str(isolated_home),
        )
        assert await mgr.interrupt(lane_id) is False

    asyncio.run(_run())


def test_codex_kind_round_trip_via_stub(isolated_home: Path) -> None:
    """Both kinds work — verify Codex stub events also translate to
    distinct LaneEvent kinds."""
    mgr = LaneManager(adapter_factory=_stub_factory)
    lane_id = asyncio.run(
        mgr.open(
            kind="codex",
            mode="headless",
            model="gpt-5",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(_send_and_drain(mgr, lane_id, "audit this"))

    events, _ = mgr.read(lane_id, None)
    kinds = [e.kind for e in events]
    assert "assistant_text" in kinds
    assert "turn_started" in kinds
    assert "turn_ended" in kinds
    persisted = read_lane(lane_id)
    assert persisted.cli_session_id == "thread-abc"
