"""X-5 Session A — lane_named_get + lane_named_ensure kernel-tool surface.

Mirror of `fix_pass_tars_cockpit_X_4/test_lane_kernel_tools.py` for the
two named-lane tools: provider-unwired path, happy path through the
real NamedLaneManager, invalid-name surfaces clean error."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.lane_named_ensure import (
    LaneNamedEnsureInput,
    LaneNamedEnsureTool,
)
from tesseract.kernel.tools.lane_named_get import (
    LaneNamedGetInput,
    LaneNamedGetTool,
)
from tesseract.orchestrator.tars_controller.lanes import (
    Lane,
    LaneManager,
    NamedLaneManager,
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
            "session_id": "sess-tool-test",
        })
        return {"session_id": "sess-tool-test", "is_error": False, "usage": {}}


def _factory(lane: Lane, runtime: LaneRuntime) -> Any:
    return _StubClaudeAdapter()


def _ctx_with_manager(manager: NamedLaneManager) -> ToolContext:
    return ToolContext(
        workspace_root=".",
        named_lane_manager_provider=lambda: manager,
    )


def test_lane_named_get_unwired_returns_clean_error() -> None:
    tool = LaneNamedGetTool()
    result = asyncio.run(
        tool.run(
            LaneNamedGetInput(name="coder/claude"),
            ToolContext(workspace_root="."),
        )
    )
    assert result.is_error
    assert "not wired" in result.output


def test_lane_named_ensure_unwired_returns_clean_error() -> None:
    tool = LaneNamedEnsureTool()
    result = asyncio.run(
        tool.run(
            LaneNamedEnsureInput(
                name="coder/claude",
                kind="claude",
                model="claude-sonnet-4-6",
                working_dir=".",
            ),
            ToolContext(workspace_root="."),
        )
    )
    assert result.is_error
    assert "not wired" in result.output


def test_lane_named_get_reports_unbound(isolated_home: Path) -> None:
    mgr = NamedLaneManager(lane_manager=LaneManager(adapter_factory=_factory))
    ctx = _ctx_with_manager(mgr)
    result = asyncio.run(
        LaneNamedGetTool().run(LaneNamedGetInput(name="coder/claude"), ctx)
    )
    assert not result.is_error
    assert result.metadata == {"name": "coder/claude", "bound": False}


def test_lane_named_ensure_then_get_round_trip(isolated_home: Path) -> None:
    mgr = NamedLaneManager(lane_manager=LaneManager(adapter_factory=_factory))
    ctx = _ctx_with_manager(mgr)
    ensure_result = asyncio.run(
        LaneNamedEnsureTool().run(
            LaneNamedEnsureInput(
                name="coder/claude",
                kind="claude",
                model="claude-sonnet-4-6",
                working_dir=str(isolated_home),
            ),
            ctx,
        )
    )
    assert not ensure_result.is_error
    lane_id = (ensure_result.metadata or {}).get("lane_id")
    assert isinstance(lane_id, str) and lane_id.startswith("lane-claude-")

    get_result = asyncio.run(
        LaneNamedGetTool().run(LaneNamedGetInput(name="coder/claude"), ctx)
    )
    assert not get_result.is_error
    assert (get_result.metadata or {}).get("bound") is True
    assert (get_result.metadata or {}).get("lane_id") == lane_id


def test_lane_named_ensure_invalid_name_clean_error(isolated_home: Path) -> None:
    mgr = NamedLaneManager(lane_manager=LaneManager(adapter_factory=_factory))
    ctx = _ctx_with_manager(mgr)
    result = asyncio.run(
        LaneNamedEnsureTool().run(
            LaneNamedEnsureInput(
                name="Bad/Name!",
                kind="claude",
                model="claude-sonnet-4-6",
                working_dir=str(isolated_home),
            ),
            ctx,
        )
    )
    assert result.is_error
    assert "invalid named-lane name" in result.output


def test_lane_named_ensure_uncataloged_model_rejected(isolated_home: Path) -> None:
    """An invented model id must be rejected before any lane opens — a
    binding that recorded ``codex-mini`` (in no catalog) failed every
    spawn with a provider 400 (2026-07-12)."""
    mgr = NamedLaneManager(lane_manager=LaneManager(adapter_factory=_factory))
    ctx = _ctx_with_manager(mgr)
    result = asyncio.run(
        LaneNamedEnsureTool().run(
            LaneNamedEnsureInput(
                name="auditor/codex",
                kind="codex",
                model="codex-mini",
                working_dir=str(isolated_home),
            ),
            ctx,
        )
    )
    assert result.is_error
    assert "not in the providers.yaml catalog" in result.output
    get_result = asyncio.run(
        LaneNamedGetTool().run(LaneNamedGetInput(name="auditor/codex"), ctx)
    )
    assert (get_result.metadata or {}).get("bound") is False


def test_lane_named_ensure_kind_mismatch_clean_error(isolated_home: Path) -> None:
    mgr = NamedLaneManager(lane_manager=LaneManager(adapter_factory=_factory))
    ctx = _ctx_with_manager(mgr)
    asyncio.run(
        LaneNamedEnsureTool().run(
            LaneNamedEnsureInput(
                name="coder/claude",
                kind="claude",
                model="claude-sonnet-4-6",
                working_dir=str(isolated_home),
            ),
            ctx,
        )
    )
    swap = asyncio.run(
        LaneNamedEnsureTool().run(
            LaneNamedEnsureInput(
                name="coder/claude",
                kind="codex",
                model="gpt-5-codex",
                working_dir=str(isolated_home),
            ),
            ctx,
        )
    )
    assert swap.is_error
    assert "bound to kind=claude" in swap.output
