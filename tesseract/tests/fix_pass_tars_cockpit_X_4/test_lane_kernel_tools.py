"""X-4 Session B — kernel-tool surface tests for the seven `lane_*`.

Each tool resolves a `LaneManager` via `ToolContext.lane_manager_provider`
and falls back to a clean error `ToolResult` when the provider is
unwired (Mirror brain / boot-failure paths). The success path drives a
real `LaneManager` against an isolated `TESSERACT_HOME` so the wire
shape end-to-end matches what the controller-side brain would see."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.lane_attach import LaneAttachInput, LaneAttachTool
from tesseract.kernel.tools.lane_close import LaneCloseInput, LaneCloseTool
from tesseract.kernel.tools.lane_list import LaneListInput, LaneListTool
from tesseract.kernel.tools.lane_open import LaneOpenInput, LaneOpenTool
from tesseract.kernel.tools.lane_read import LaneReadInput, LaneReadTool
from tesseract.kernel.tools.lane_send import LaneSendInput, LaneSendTool
from tesseract.kernel.tools.lane_status import LaneStatusInput, LaneStatusTool
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
        on_event({"type": "system", "subtype": "init", "session_id": "sess-tool-test"})
        on_event({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "tool-test reply"}]},
        })
        on_event({"type": "result", "subtype": "success", "result": "", "usage": {}})
        return {"session_id": "sess-tool-test", "is_error": False, "usage": {}}


def _stub_factory(lane: Lane, runtime: LaneRuntime) -> Any:
    return _StubAdapter()


async def _send_and_drain(mgr: LaneManager, lane_id: str, message: str) -> None:
    """send is fire-and-queue — drain inside the same event loop so the
    turn task completes before assertions."""
    result = await mgr.send(lane_id, message)
    assert result.accepted
    await mgr.drain(lane_id)


@pytest.fixture
def manager_ctx(isolated_home: Path) -> tuple[LaneManager, ToolContext]:
    mgr = LaneManager(adapter_factory=_stub_factory)
    ctx = ToolContext(
        workspace_root=str(isolated_home),
        session_id="x4b-test",
        lane_manager_provider=lambda: mgr,
    )
    return mgr, ctx


# ---------------------------------------------------------------- open + list


def test_lane_open_returns_lane_id(
    manager_ctx: tuple[LaneManager, ToolContext], isolated_home: Path
) -> None:
    _, ctx = manager_ctx
    result = asyncio.run(
        LaneOpenTool().run(
            LaneOpenInput(
                kind="claude",
                mode="headless",
                model="claude-sonnet-4-6",
                working_dir=str(isolated_home),
            ),
            ctx,
        )
    )
    assert not result.is_error, result.output
    assert result.metadata is not None
    assert result.metadata["lane_id"].startswith("lane-claude-")


def test_lane_open_clean_error_when_provider_returns_none(
    isolated_home: Path,
) -> None:
    ctx = ToolContext(
        workspace_root=str(isolated_home),
        lane_manager_provider=lambda: None,
    )
    result = asyncio.run(
        LaneOpenTool().run(
            LaneOpenInput(
                kind="claude",
                mode="headless",
                model="claude-sonnet-4-6",
                working_dir=str(isolated_home),
            ),
            ctx,
        )
    )
    assert result.is_error
    assert "not wired" in result.output


def test_lane_open_uncataloged_model_rejected(
    manager_ctx: tuple[LaneManager, ToolContext], isolated_home: Path
) -> None:
    """An invented model id must be rejected before a CLI spawns — it
    would be passed verbatim as --model and fail every send with a
    provider 400 (codex-mini incident, 2026-07-12)."""
    mgr, ctx = manager_ctx
    result = asyncio.run(
        LaneOpenTool().run(
            LaneOpenInput(
                kind="codex",
                mode="headless",
                model="codex-mini",
                working_dir=str(isolated_home),
            ),
            ctx,
        )
    )
    assert result.is_error
    assert "not in the providers.yaml catalog" in result.output
    assert mgr.list_ids() == []


def test_lane_list_returns_open_lane_ids(
    manager_ctx: tuple[LaneManager, ToolContext], isolated_home: Path
) -> None:
    mgr, ctx = manager_ctx
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    result = asyncio.run(LaneListTool().run(LaneListInput(), ctx))
    assert not result.is_error
    assert result.metadata is not None
    assert lane_id in result.metadata["ids"]



def test_lane_list_empty(
    manager_ctx: tuple[LaneManager, ToolContext], isolated_home: Path
) -> None:
    _, ctx = manager_ctx
    result = asyncio.run(LaneListTool().run(LaneListInput(), ctx))
    assert not result.is_error
    assert result.output == "(no lanes)"


# ---------------------------------------------------------------- send + read


def test_lane_send_accepted(
    manager_ctx: tuple[LaneManager, ToolContext], isolated_home: Path
) -> None:
    mgr, ctx = manager_ctx
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    async def _send() -> Any:
        result = await LaneSendTool().run(
            LaneSendInput(lane_id=lane_id, message="hello"), ctx
        )
        await mgr.drain(lane_id)  # settle the queued turn before loop close
        return result

    result = asyncio.run(_send())
    assert not result.is_error, result.output
    assert result.metadata is not None
    assert result.metadata["accepted"] is True
    # Fire-and-queue: the ack's depth includes the turn just queued.
    assert result.metadata["queue_depth"] == 1


def test_lane_read_returns_events(
    manager_ctx: tuple[LaneManager, ToolContext], isolated_home: Path
) -> None:
    mgr, ctx = manager_ctx
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(_send_and_drain(mgr, lane_id, "hello"))
    result = asyncio.run(
        LaneReadTool().run(LaneReadInput(lane_id=lane_id, since_cursor=None), ctx)
    )
    assert not result.is_error
    assert result.metadata is not None
    assert result.metadata["count"] >= 4
    assert int(result.metadata["next_cursor"]) > 0


# ---------------------------------------------------------------- status + attach


def test_lane_status_post_send(
    manager_ctx: tuple[LaneManager, ToolContext], isolated_home: Path
) -> None:
    mgr, ctx = manager_ctx
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(_send_and_drain(mgr, lane_id, "ping"))
    result = asyncio.run(
        LaneStatusTool().run(LaneStatusInput(lane_id=lane_id), ctx)
    )
    assert not result.is_error
    assert result.metadata is not None
    assert result.metadata["alive"] is True
    assert result.metadata["busy"] is False


def test_lane_attach_round_trip(
    manager_ctx: tuple[LaneManager, ToolContext], isolated_home: Path
) -> None:
    mgr, ctx = manager_ctx
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    asyncio.run(_send_and_drain(mgr, lane_id, "test"))
    result = asyncio.run(
        LaneAttachTool().run(LaneAttachInput(lane_id=lane_id), ctx)
    )
    assert not result.is_error
    assert result.metadata is not None
    assert result.metadata["lane"]["lane_id"] == lane_id
    assert len(result.metadata["recent_events"]) >= 4


# ---------------------------------------------------------------- close


def test_lane_close_returns_archive_path(
    manager_ctx: tuple[LaneManager, ToolContext], isolated_home: Path
) -> None:
    mgr, ctx = manager_ctx
    lane_id = asyncio.run(
        mgr.open(
            kind="claude",
            mode="headless",
            model="claude-sonnet-4-6",
            working_dir=str(isolated_home),
        )
    )
    result = asyncio.run(
        LaneCloseTool().run(
            LaneCloseInput(lane_id=lane_id, reason="operator_close"), ctx
        )
    )
    assert not result.is_error, result.output
    assert result.metadata is not None
    assert result.metadata["final_status"] == "closed"
    assert "archive_dir" in result.metadata


# ---------------------------------------------------------------- provider none


def test_every_tool_surfaces_provider_none_cleanly(isolated_home: Path) -> None:
    """Mirror's brain hits this path until Session C — every tool must
    return `is_error=True` with a "not wired" message, never raise."""
    ctx = ToolContext(workspace_root=str(isolated_home))  # no provider
    cases = [
        (LaneListTool(), LaneListInput()),
        (LaneStatusTool(), LaneStatusInput(lane_id="lane-x")),
        (LaneReadTool(), LaneReadInput(lane_id="lane-x")),
        (LaneAttachTool(), LaneAttachInput(lane_id="lane-x")),
        (LaneSendTool(), LaneSendInput(lane_id="lane-x", message="m")),
        (LaneCloseTool(), LaneCloseInput(lane_id="lane-x", reason="operator_close")),
        (
            LaneOpenTool(),
            LaneOpenInput(
                kind="claude", mode="headless", model="m", working_dir="/tmp"
            ),
        ),
    ]
    for tool, inp in cases:
        result = asyncio.run(tool.run(inp, ctx))
        assert result.is_error, f"{tool.name} should error when provider missing"
        assert "not wired" in result.output, tool.name


# ---------------------------------------------------------------- unknown lane


def test_lane_attach_unknown_id_returns_clean_error(
    manager_ctx: tuple[LaneManager, ToolContext],
) -> None:
    """Reviewer Important #10 — attach is the brain-restart recovery
    primitive; a misfire on an unknown id is highest-stakes. The tool
    must convert `FileNotFoundError` from `read_lane` into a clean
    error `ToolResult`, never raise."""
    _, ctx = manager_ctx
    result = asyncio.run(
        LaneAttachTool().run(LaneAttachInput(lane_id="lane-claude-nonexistent"), ctx)
    )
    assert result.is_error
    assert "lane_attach failed" in result.output


def test_lane_status_unknown_id_returns_clean_error(
    manager_ctx: tuple[LaneManager, ToolContext],
) -> None:
    """Same shape for status — `read_lane` raises FileNotFoundError on
    missing record; tool wraps in clean is_error=True."""
    _, ctx = manager_ctx
    result = asyncio.run(
        LaneStatusTool().run(LaneStatusInput(lane_id="lane-claude-nonexistent"), ctx)
    )
    assert result.is_error
    assert "lane_status failed" in result.output


def test_lane_send_unknown_id_returns_clean_error(
    manager_ctx: tuple[LaneManager, ToolContext],
) -> None:
    """`lane_send` requires an attached runtime; a never-opened id has
    none → `LaneNotFoundError` from `_require_runtime`. Tool surfaces
    that as a clean is_error=True instead of raising."""
    _, ctx = manager_ctx
    result = asyncio.run(
        LaneSendTool().run(
            LaneSendInput(lane_id="lane-claude-nonexistent", message="m"), ctx
        )
    )
    assert result.is_error
    assert "lane_send failed" in result.output
