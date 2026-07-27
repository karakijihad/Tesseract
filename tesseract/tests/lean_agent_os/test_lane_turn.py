"""lane_turn — composite send + await turn_ended + reply, one call.

Collapses the lane_send -> poll lane_read -> parse dance into a single
tool call. Fakes model the two manager shapes lane_turn must work
against generically (real in-process LaneManager: sync read, async
send; Mirror IpcLaneManager: everything async) via `maybe_await`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.lane_turn import LaneTurnInput, LaneTurnTool
from tesseract.orchestrator.tars_controller.lanes.models import LaneEvent, LaneSendResult
from tesseract.orchestrator.tars_controller.lanes.named import NamedLaneRecord


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _fast_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic, fast polling — no dependency on real cockpit.yaml
    timing so the test suite doesn't slow down or flake on shared CI."""
    monkeypatch.setattr(
        "tesseract.kernel.tools.lane_turn.load_conductor_relay",
        lambda: (0.0, 5.0),
    )
    monkeypatch.setattr(
        "tesseract.kernel.tools.lane_turn.load_conductor_reply_cap",
        lambda: 8000,
    )


def _ev(lane_id: str, kind: str, cursor: str, payload: dict[str, Any] | None = None) -> LaneEvent:
    return LaneEvent(lane_id=lane_id, kind=kind, payload=payload or {}, cursor=cursor)


class _HappyPathManager:
    """read() is sync (real LaneManager shape); send() is async."""

    def __init__(self, lane_id: str) -> None:
        self.lane_id = lane_id
        self.reads = 0

    def read(self, lane_id: str, since_cursor: str | None = None):
        self.reads += 1
        if since_cursor is None:
            return [], "0"
        if since_cursor == "0":
            return [_ev(lane_id, "assistant_text", "1", {"text": "Hello from lane"})], "1"
        return [_ev(lane_id, "turn_ended", "2", {"is_error": False})], "2"

    async def send(self, lane_id: str, message: str) -> LaneSendResult:
        return LaneSendResult(accepted=True, queue_depth=0)


def test_happy_path_returns_reply_and_turn_completed() -> None:
    mgr = _HappyPathManager("lane-claude-1")
    ctx = ToolContext(workspace_root=".", lane_manager_provider=lambda: mgr)
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(name_or_id="lane-claude-1", message="hello"), ctx
        )
    )
    assert not result.is_error, result.output
    assert result.timed_out is False
    assert result.metadata is not None
    assert result.metadata["turn_completed"] is True
    assert result.metadata["reply_text"] == "Hello from lane"
    assert result.metadata["lane_id"] == "lane-claude-1"
    assert result.metadata["cursor"] == "2"
    assert "turn_ended" in result.metadata["tool_activity_summary"]
    assert "Hello from lane" in result.output


class _TurnErrorsManager:
    """The turn completes, but the CLI agent itself errored — turn_ended
    carries payload.is_error=True (manager.py:258-268 shape)."""

    def read(self, lane_id: str, since_cursor: str | None = None):
        if since_cursor is None:
            return [], "0"
        return [_ev(lane_id, "turn_ended", "1", {"is_error": True})], "1"

    async def send(self, lane_id: str, message: str) -> LaneSendResult:
        return LaneSendResult(accepted=True, queue_depth=0)


def test_turn_ended_is_error_propagates_to_tool_result() -> None:
    """Reviewer finding: a completed turn that ended in error must set
    ToolResult.is_error=True, not just bury it in tool_activity_summary —
    otherwise a caller branching on result.is_error can't tell success
    from a failed CLI turn."""
    mgr = _TurnErrorsManager()
    ctx = ToolContext(workspace_root=".", lane_manager_provider=lambda: mgr)
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(name_or_id="lane-claude-err", message="hello"), ctx
        )
    )
    assert result.is_error
    assert result.metadata is not None
    assert result.metadata["turn_completed"] is True


class _StallsManager:
    """Emits a couple of partial events then goes silent without ever
    emitting turn_ended — a stalled lane. Exercises the stall-timeout
    path (timeout_s bounds SILENCE, not total turn duration)."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self, lane_id: str, since_cursor: str | None = None):
        self.reads += 1
        if since_cursor is None:
            return [], "0"
        n = self.reads
        if n <= 2:
            return [
                _ev(lane_id, "assistant_text_partial", str(n), {"text": f"chunk-{n}"})
            ], str(n)
        return [], since_cursor  # silence — the lane has stalled

    async def send(self, lane_id: str, message: str) -> LaneSendResult:
        return LaneSendResult(accepted=True, queue_depth=0)


def test_stall_returns_partial_with_cursor_and_not_completed() -> None:
    mgr = _StallsManager()
    ctx = ToolContext(workspace_root=".", lane_manager_provider=lambda: mgr)
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(
                name_or_id="lane-claude-2", message="hello", timeout_s=0.02
            ),
            ctx,
        )
    )
    assert not result.is_error, result.output
    assert result.timed_out is True
    assert result.metadata is not None
    assert result.metadata["turn_completed"] is False
    assert result.metadata["lane_id"] == "lane-claude-2"
    assert isinstance(result.metadata["cursor"], str) and result.metadata["cursor"]
    # No assistant_text (only *_partial) landed -> reply_text stays empty.
    assert result.metadata["reply_text"] == ""
    # Reviewer finding: only result.output reaches the model as tool-message
    # content, so the timeout marker must live there, not just in metadata.
    assert "[turn not finished" in result.output
    assert f"cursor={result.metadata['cursor']}" in result.output


class _SlowButAliveManager:
    """Every read takes longer than timeout_s WOULD allow in total, but
    each gap is under the stall ceiling — an active long turn. The
    2026-07-13 incident regression guard: a wall-clock deadline here
    abandoned healthy long turns; activity must extend the wait."""

    def __init__(self, gap_s: float, events_before_end: int) -> None:
        self.gap_s = gap_s
        self.events_before_end = events_before_end
        self.reads = 0

    async def read(self, lane_id: str, since_cursor: str | None = None):
        if since_cursor is None:
            return [], "0"
        await asyncio.sleep(self.gap_s)
        self.reads += 1
        n = self.reads
        if n <= self.events_before_end:
            return [_ev(lane_id, "tool_use", str(n), {"name": f"tool-{n}"})], str(n)
        return [_ev(lane_id, "turn_ended", str(n), {"is_error": False})], str(n)

    async def send(self, lane_id: str, message: str) -> LaneSendResult:
        return LaneSendResult(accepted=True, queue_depth=0)


def test_active_turn_longer_than_timeout_still_completes() -> None:
    """Total turn duration (10 × 0.02 s gaps ≈ 0.2 s) exceeds timeout_s
    (0.15 s), but no single silence does — the deadline must reset on
    each batch of events and the turn must complete. Margins sized for
    Windows' ~15 ms timer resolution (review 2026-07-14): a gap may
    stretch to ~0.05 s under load and must stay well under the ceiling."""
    mgr = _SlowButAliveManager(gap_s=0.02, events_before_end=9)
    ctx = ToolContext(workspace_root=".", lane_manager_provider=lambda: mgr)
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(
                name_or_id="lane-claude-slow", message="hello", timeout_s=0.15
            ),
            ctx,
        )
    )
    assert not result.is_error, result.output
    assert result.timed_out is False
    assert result.metadata is not None
    assert result.metadata["turn_completed"] is True


def test_happy_path_output_has_no_timeout_marker() -> None:
    mgr = _HappyPathManager("lane-claude-1")
    ctx = ToolContext(workspace_root=".", lane_manager_provider=lambda: mgr)
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(name_or_id="lane-claude-1", message="hello"), ctx
        )
    )
    assert "[turn not finished" not in result.output


class _GoneManager:
    def read(self, lane_id: str, since_cursor: str | None = None):
        raise FileNotFoundError(f"lane {lane_id} not found")

    async def send(self, lane_id: str, message: str) -> LaneSendResult:
        raise AssertionError("must not send when the pre-send read already failed")


def test_lane_gone_returns_clean_error() -> None:
    mgr = _GoneManager()
    ctx = ToolContext(workspace_root=".", lane_manager_provider=lambda: mgr)
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(name_or_id="lane-claude-ghost", message="hello"), ctx
        )
    )
    assert result.is_error
    assert "lane_turn failed" in result.output


class _DetachedUntilAttachedManager:
    """Post-restart shape (M6): a disk-alive lane is detached until an explicit
    attach(); the first read/send raise 'not attached', attach() flips it."""

    def __init__(self, lane_id: str) -> None:
        self.attached = False
        self.attach_calls = 0
        self._happy = _HappyPathManager(lane_id)

    def read(self, lane_id: str, since_cursor: str | None = None):
        if not self.attached:
            raise RuntimeError(f"lane {lane_id} is not attached; call attach() first")
        return self._happy.read(lane_id, since_cursor)

    async def send(self, lane_id: str, message: str) -> LaneSendResult:
        if not self.attached:
            raise RuntimeError(f"lane {lane_id} is not attached; call attach() first")
        return await self._happy.send(lane_id, message)

    async def attach(self, lane_id: str):
        self.attach_calls += 1
        self.attached = True
        return None


def test_lane_turn_self_heals_when_not_attached() -> None:
    """M6: the default trio path (raw lane_turn) must attach + retry once when a
    daemon restart left the named lane detached — not surface the raw error."""
    mgr = _DetachedUntilAttachedManager("lane-claude-restart")
    ctx = ToolContext(workspace_root=".", lane_manager_provider=lambda: mgr)
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(name_or_id="lane-claude-restart", message="hello"), ctx
        )
    )
    assert not result.is_error, result.output
    assert mgr.attach_calls == 1
    assert result.metadata is not None
    assert result.metadata["turn_completed"] is True
    assert result.metadata["reply_text"] == "Hello from lane"


def test_lane_turn_not_attached_reattach_failure_is_clean_error() -> None:
    """If the one-shot re-attach itself fails, surface a clean error."""

    class _AttachFailsManager(_DetachedUntilAttachedManager):
        async def attach(self, lane_id: str):
            self.attach_calls += 1
            raise RuntimeError("daemon still down")

    mgr = _AttachFailsManager("lane-claude-restart")
    ctx = ToolContext(workspace_root=".", lane_manager_provider=lambda: mgr)
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(name_or_id="lane-claude-restart", message="hello"), ctx
        )
    )
    assert result.is_error
    assert mgr.attach_calls == 1
    assert "lane_turn failed" in result.output


class _RejectManager:
    def read(self, lane_id: str, since_cursor: str | None = None):
        return [], "0"

    async def send(self, lane_id: str, message: str) -> LaneSendResult:
        return LaneSendResult(accepted=False, queue_depth=3, reason="busy")


def test_send_rejected_returns_clean_error() -> None:
    mgr = _RejectManager()
    ctx = ToolContext(workspace_root=".", lane_manager_provider=lambda: mgr)
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(name_or_id="lane-claude-3", message="hello"), ctx
        )
    )
    assert result.is_error
    assert "rejected" in result.output
    assert result.metadata is not None
    assert result.metadata["accepted"] is False
    assert result.metadata["queue_depth"] == 3


class _FakeNamedManager:
    def __init__(self, record: NamedLaneRecord | None) -> None:
        self._record = record

    def get(self, name: str) -> NamedLaneRecord | None:
        if self._record is not None and name == self._record.name:
            return self._record
        return None


def test_named_resolution_maps_name_to_lane_id() -> None:
    record = NamedLaneRecord(
        name="coder/claude",
        lane_id="lane-claude-bound",
        kind="claude",
        model="test-model",
        working_dir=".",
    )
    lane_mgr = _HappyPathManager("lane-claude-bound")
    named_mgr = _FakeNamedManager(record)
    ctx = ToolContext(
        workspace_root=".",
        lane_manager_provider=lambda: lane_mgr,
        named_lane_manager_provider=lambda: named_mgr,
    )
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(name_or_id="coder/claude", message="hello"), ctx
        )
    )
    assert not result.is_error, result.output
    assert result.metadata is not None
    assert result.metadata["lane_id"] == "lane-claude-bound"
    assert result.metadata["turn_completed"] is True


def test_unbound_name_falls_back_to_raw_lane_id() -> None:
    """No named binding exists for the given name -> name_or_id is used
    as-is (a raw lane_id), matching lane_send's contract."""
    lane_mgr = _HappyPathManager("lane-claude-raw")
    named_mgr = _FakeNamedManager(None)
    ctx = ToolContext(
        workspace_root=".",
        lane_manager_provider=lambda: lane_mgr,
        named_lane_manager_provider=lambda: named_mgr,
    )
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(name_or_id="lane-claude-raw", message="hello"), ctx
        )
    )
    assert not result.is_error, result.output
    assert result.metadata is not None
    assert result.metadata["lane_id"] == "lane-claude-raw"


def test_provider_unwired_returns_clean_error() -> None:
    ctx = ToolContext(workspace_root=".")
    result = asyncio.run(
        LaneTurnTool().run(
            LaneTurnInput(name_or_id="lane-claude-x", message="hello"), ctx
        )
    )
    assert result.is_error
    assert "not wired" in result.output
