"""X-5 Session A — two named lanes persist across brain restart.

The tmux Agent Teams pattern: TARS holds `coder/claude` +
`auditor/codex` as long-lived lanes. After a brain restart, both
bindings re-resolve to the SAME `lane_id` they had pre-restart, and
each lane's prior history is still attachable.

Simulation: build mgr_a → ensure both names → drop everything →
build mgr_b against same on-disk state → ensure with same args →
expect identical lane_ids + attachable history. A true subprocess
restart (kill brain OS process while daemon stays alive) lives in
Session B once the daemon IPC bridge for named verbs lands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from tesseract.orchestrator.tars_controller.lanes import (
    Lane,
    LaneManager,
    NamedLaneManager,
)
from tesseract.orchestrator.tars_controller.lanes.manager import LaneRuntime


class _StubClaudeAdapter:
    def __init__(self, session_id: str = "sess-claude-x5") -> None:
        self._session_id = session_id

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
            "session_id": self._session_id,
        })
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"claude: {message}"}]},
        })
        on_event({"type": "result", "subtype": "success", "result": "", "usage": {}})
        return {"session_id": self._session_id, "is_error": False, "usage": {}}


class _StubCodexAdapter:
    def __init__(self, session_id: str = "sess-codex-x5") -> None:
        self._session_id = session_id

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        on_event({"type": "thread.started", "thread_id": self._session_id})
        on_event({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": f"codex: {message}"},
        })
        return {"session_id": self._session_id, "is_error": False, "usage": {}}


def _factory(lane: Lane, runtime: LaneRuntime) -> Any:
    if lane.kind == "claude":
        return _StubClaudeAdapter()
    return _StubCodexAdapter()


def _build_pair(home: Path) -> NamedLaneManager:
    return NamedLaneManager(lane_manager=LaneManager(adapter_factory=_factory))


def test_two_named_lanes_persist_across_brain_restart(
    isolated_home: Path,
) -> None:
    """Open coder/claude + auditor/codex via mgr_a, drop the in-memory
    state, rebuild mgr_b from disk, re-ensure with the same args. Both
    must reuse the pre-restart lane_ids — that's the whole binding
    contract."""
    mgr_a = _build_pair(isolated_home)
    coder_a = asyncio.run(
        mgr_a.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    auditor_a = asyncio.run(
        mgr_a.ensure(
            "auditor/codex",
            kind="codex",
            model="gpt-5-codex",
            working_dir=str(isolated_home),
        )
    )
    # Drive a turn on each so the lane has real history to recover.
    asyncio.run(mgr_a.lane_manager.send(coder_a.lane_id, "first coder turn"))
    asyncio.run(mgr_a.lane_manager.send(auditor_a.lane_id, "first auditor turn"))
    del mgr_a

    mgr_b = _build_pair(isolated_home)
    coder_b = asyncio.run(
        mgr_b.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    auditor_b = asyncio.run(
        mgr_b.ensure(
            "auditor/codex",
            kind="codex",
            model="gpt-5-codex",
            working_dir=str(isolated_home),
        )
    )

    assert coder_b.lane_id == coder_a.lane_id
    assert auditor_b.lane_id == auditor_a.lane_id

    coder_snap = asyncio.run(mgr_b.lane_manager.attach(coder_b.lane_id))
    auditor_snap = asyncio.run(mgr_b.lane_manager.attach(auditor_b.lane_id))
    # 4 events each: status_change + turn_started + assistant_text + turn_ended.
    assert len(coder_snap.recent_events) >= 4
    assert len(auditor_snap.recent_events) >= 4


def test_named_lane_record_persisted_to_disk(isolated_home: Path) -> None:
    """The binding file lands under `<TESSERACT_HOME>/controller/named-lanes/`
    with the slash-sanitised filename."""
    mgr = _build_pair(isolated_home)
    asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    expected = isolated_home / "controller" / "named-lanes" / "coder__claude.json"
    assert expected.exists()


def test_ensure_is_idempotent_within_same_manager(isolated_home: Path) -> None:
    """Calling ensure twice without restart returns the same lane_id —
    binding reuse, not a second open."""
    mgr = _build_pair(isolated_home)
    a = asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    b = asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    assert a.lane_id == b.lane_id


def test_ensure_reopens_when_bound_lane_was_closed(isolated_home: Path) -> None:
    """If the bound lane was explicitly closed (dead), ensure opens a
    fresh lane under the SAME name — stale-binding repair. The old
    lane_id is replaced; the binding file is rewritten."""
    mgr = _build_pair(isolated_home)
    first = asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(mgr.lane_manager.close(first.lane_id, reason="operator_close"))

    second = asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    assert second.lane_id != first.lane_id
    # Disk record agrees with the fresh lane_id.
    on_disk = mgr.get("coder/claude")
    assert on_disk is not None
    assert on_disk.lane_id == second.lane_id


def test_kind_mismatch_raises_rather_than_swap(isolated_home: Path) -> None:
    """A name bound to claude cannot be re-pointed at codex without
    explicit release first — guardrail against silently routing
    coder/claude work into a Codex lane mid-flight."""
    import pytest

    from tesseract.orchestrator.tars_controller.lanes.named import (
        NamedLaneError,
    )

    mgr = _build_pair(isolated_home)
    asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    with pytest.raises(NamedLaneError, match="bound to kind=claude"):
        asyncio.run(
            mgr.ensure(
                "coder/claude",
                kind="codex",
                model="gpt-5-codex",
                working_dir=str(isolated_home),
            )
        )


def test_release_drops_binding_without_closing_lane(isolated_home: Path) -> None:
    """release(name) removes the binding file but leaves the underlying
    lane (LaneManager-owned) alive. A subsequent ensure opens a new
    lane id; the old lane is still queryable until closed explicitly."""
    mgr = _build_pair(isolated_home)
    first = asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    assert mgr.release("coder/claude") is True
    # Same kind allowed again — binding is gone.
    second = asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    assert second.lane_id != first.lane_id
    # The original lane is still alive in the LaneManager.
    assert first.lane_id in mgr.lane_manager.list_ids()


def test_list_returns_all_bindings(isolated_home: Path) -> None:
    mgr = _build_pair(isolated_home)
    asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(
        mgr.ensure(
            "auditor/codex",
            kind="codex",
            model="gpt-5-codex",
            working_dir=str(isolated_home),
        )
    )
    names = sorted(r.name for r in mgr.list())
    assert names == ["auditor/codex", "coder/claude"]


def test_concurrent_ensure_for_same_name_does_not_orphan_a_lane(
    isolated_home: Path,
) -> None:
    """Two concurrent `ensure` calls for the same name MUST resolve to
    the same `lane_id`. Without the per-name lock the second call would
    `open` a fresh lane and overwrite the binding, leaving the first
    lane orphaned in `controller/lanes/` with no name pointing at it."""
    mgr = _build_pair(isolated_home)

    async def _go() -> tuple[str, str, int]:
        rec_a, rec_b = await asyncio.gather(
            mgr.ensure(
                "coder/claude",
                kind="claude",
                model="claude-sonnet-4-6",
                working_dir=str(isolated_home),
            ),
            mgr.ensure(
                "coder/claude",
                kind="claude",
                model="claude-sonnet-4-6",
                working_dir=str(isolated_home),
            ),
        )
        return rec_a.lane_id, rec_b.lane_id, len(mgr.lane_manager.list_ids())

    a_id, b_id, total_lanes = asyncio.run(_go())
    assert a_id == b_id
    assert total_lanes == 1  # no orphan


def test_invalid_name_raises(isolated_home: Path) -> None:
    import pytest

    from tesseract.orchestrator.tars_controller.lanes.named import (
        InvalidNamedLaneNameError,
    )

    mgr = _build_pair(isolated_home)
    with pytest.raises(InvalidNamedLaneNameError):
        asyncio.run(
            mgr.ensure(
                "Coder/Claude!",  # uppercase + punctuation blocked
                kind="claude",
                model="claude-sonnet-4-6",
                working_dir=str(isolated_home),
            )
        )
    with pytest.raises(InvalidNamedLaneNameError):
        mgr.get("a/b/c")  # only one slash allowed
