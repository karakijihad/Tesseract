"""X-5 Session B — autonomy dispatch through named lanes.

When `KernelWorkerRunner` is constructed with a `named_lane_manager_provider`,
CLAUDE_CLI records MUST route to the named lane `coder/claude` and CODEX_CLI
records MUST route to `auditor/codex` — both via `NamedLaneManager.ensure` +
`LaneManager.send` rather than the unconditional `delegate_claude` /
`delegate_codex` tool path.

Without the named lane manager, dispatch falls back to the existing
`delegate_*` path (backward compatibility — callers that don't wire a
named lane manager continue to work as before).

Design contract (from phase-X-5-persistent-lanes.md §5):
  - Code-editing work-kinds (CLAUDE_CLI) → `coder/claude` lane
  - Review/audit work-kinds (CODEX_CLI) → `auditor/codex` lane
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from tesseract.brain.tools import ToolRegistry
from tesseract.orchestrator.autonomy.kernel_worker_runner import KernelWorkerRunner
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.tars_controller.lanes import LaneManager, NamedLaneManager
from tesseract.orchestrator.tars_controller.lanes.manager import LaneRuntime
from tesseract.orchestrator.tars_controller.lanes.models import Lane
from tesseract.orchestrator.workers.record import RiskClass, WorkerRecord, WorkerStatus


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

_CODER_MODEL = "claude-test-model"
_AUDITOR_MODEL = "codex-test-model"
_WORKING_DIR = "."


@pytest.fixture(autouse=True)
def _stub_trio_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner resolves the lane model from cockpit.yaml -> roles.yaml at
    dispatch time; stub it so the test never reads the real config."""
    from tesseract.config import cockpit

    lanes = {
        "coder/claude": {"name": "coder/claude", "kind": "claude", "model": _CODER_MODEL},
        "auditor/codex": {"name": "auditor/codex", "kind": "codex", "model": _AUDITOR_MODEL},
    }
    monkeypatch.setattr(cockpit, "trio_lane", lambda name: lanes[name])


class _TrackingClaudeAdapter:
    """Records messages sent to it so the test can assert routing."""

    def __init__(self) -> None:
        self.received: list[str] = []

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        self.received.append(message)
        on_event({"type": "system", "subtype": "init", "session_id": "coder-sess"})
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"coder: {message}"}]},
        })
        on_event({"type": "result", "subtype": "success", "result": "", "usage": {}})
        return {"session_id": "coder-sess", "is_error": False, "usage": {}}


class _TrackingCodexAdapter:
    """Records messages sent to it."""

    def __init__(self) -> None:
        self.received: list[str] = []

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        self.received.append(message)
        on_event({"type": "thread.started", "thread_id": "auditor-thread"})
        on_event({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": f"codex: {message}"},
        })
        return {"session_id": "auditor-thread", "is_error": False, "usage": {}}


_claude_adapter = _TrackingClaudeAdapter()
_codex_adapter = _TrackingCodexAdapter()


def _factory(lane: Lane, runtime: LaneRuntime) -> Any:
    if lane.kind == "claude":
        return _claude_adapter
    return _codex_adapter


def _make_named_manager() -> NamedLaneManager:
    return NamedLaneManager(lane_manager=LaneManager(adapter_factory=_factory))


def _stub_registry() -> ToolRegistry:
    """Minimal registry — autonomy dispatch through named lanes shouldn't
    call delegate_claude/codex, so the registry can be effectively empty."""
    registry = ToolRegistry()
    return registry


def _make_record(kind: WorkerKind, prompt: str = "do the work") -> WorkerRecord:
    now = datetime.now(timezone.utc)
    return WorkerRecord(
        id=f"w-{kind.value}-test",
        kind=kind,
        created_at=now,
        updated_at=now,
        agenda_item_id="agenda-1",
        risk_class=RiskClass.AUTONOMOUS,
        role="",
        prompt=prompt,
    )


# ---------------------------------------------------------------------------
# Tests — named lane routing
# ---------------------------------------------------------------------------


def test_claude_cli_routes_to_coder_named_lane(isolated_home: Path) -> None:
    """CLAUDE_CLI with a named lane manager wired → message arrives at the
    `coder/claude` lane, NOT through delegate_claude."""
    mgr = _make_named_manager()
    # Pre-open coder lane so the runner finds a live binding.
    asyncio.run(
        mgr.ensure(
            "coder/claude",
            kind="claude",
            model=_CODER_MODEL,
            working_dir=_WORKING_DIR,
        )
    )
    runner = KernelWorkerRunner(
        _stub_registry(),
        workspace_root=str(isolated_home),
        named_lane_manager_provider=lambda: mgr,
    )
    record = _make_record(WorkerKind.CLAUDE_CLI, "implement feature X")
    asyncio.run(runner.run(record))

    assert record.status == WorkerStatus.DONE, record.summary
    assert "implement feature X" in _claude_adapter.received


def test_codex_cli_routes_to_auditor_named_lane(isolated_home: Path) -> None:
    """CODEX_CLI with a named lane manager wired → message arrives at the
    `auditor/codex` lane."""
    mgr = _make_named_manager()
    asyncio.run(
        mgr.ensure(
            "auditor/codex",
            kind="codex",
            model=_AUDITOR_MODEL,
            working_dir=_WORKING_DIR,
        )
    )
    runner = KernelWorkerRunner(
        _stub_registry(),
        workspace_root=str(isolated_home),
        named_lane_manager_provider=lambda: mgr,
    )
    record = _make_record(WorkerKind.CODEX_CLI, "review the diff")
    asyncio.run(runner.run(record))

    assert record.status == WorkerStatus.DONE, record.summary
    assert "review the diff" in _codex_adapter.received


def test_no_named_lane_manager_falls_back_to_delegate(isolated_home: Path) -> None:
    """Without named_lane_manager_provider the runner must fall back to the
    existing delegate_claude / delegate_codex path (backward compat)."""
    # We can't easily test a full delegate_claude round-trip here without
    # wiring the actual tool, so we verify the runner tries the tool path
    # by observing a clean FAILED(tool_unavailable) — meaning it reached
    # execute_tool but the stub registry had no delegate_claude entry.
    runner = KernelWorkerRunner(
        _stub_registry(),
        workspace_root=str(isolated_home),
        # No named_lane_manager_provider
    )
    record = _make_record(WorkerKind.CLAUDE_CLI, "task without named lane")
    asyncio.run(runner.run(record))
    # The tool is missing from stub registry → FAILED with the exact marker
    # "unknown tool: delegate_claude" — proves the fallback path tried the
    # delegate_* route, not the lane route (which would have raised a
    # different shape entirely).
    assert record.status == WorkerStatus.FAILED
    assert "delegate_claude" in record.summary
    assert "unknown tool" in record.summary.lower()


def test_named_lane_not_yet_open_auto_opens_it(isolated_home: Path) -> None:
    """If the named lane doesn't exist yet when the runner dispatches, the
    runner auto-opens it via `ensure` (no pre-open step required)."""
    mgr = _make_named_manager()
    runner = KernelWorkerRunner(
        _stub_registry(),
        workspace_root=str(isolated_home),
        named_lane_manager_provider=lambda: mgr,
    )
    record = _make_record(WorkerKind.CLAUDE_CLI, "cold-start task")
    asyncio.run(runner.run(record))

    # The lane was opened on demand, with the config-resolved model —
    # not a hardcoded id (roles are pillars).
    binding = mgr.get("coder/claude")
    assert binding is not None
    assert binding.model == _CODER_MODEL
    assert record.status == WorkerStatus.DONE, record.summary
