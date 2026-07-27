"""parallel-tars P4 — per-session concurrent spawn cap.

Registry-level: register past the cap raises SpawnCapExceeded (and the
rejected coroutine is closed, no un-awaited warning). Tool-level: the
background branch maps the exception to a drain-first error ToolResult.
Config-level: the loader raises loudly on a missing key.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tesseract.brain.spawns import SpawnRegistry
from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_max_concurrent_spawns_per_session,
)
from tesseract.kernel.tools.base import (
    SpawnCapExceeded,
    ToolContext,
    ToolResult,
)


async def _never_done() -> ToolResult:
    await asyncio.Event().wait()
    return ToolResult(output="unreachable")


@pytest.mark.asyncio
async def test_register_past_cap_raises():
    registry = SpawnRegistry()
    registry.max_concurrent = 2
    h1 = registry.register(kind="delegate_claude", coro=_never_done())
    h2 = registry.register(kind="delegate_codex", coro=_never_done())

    with pytest.raises(SpawnCapExceeded) as exc_info:
        registry.register(kind="delegate_claude", coro=_never_done())
    assert exc_info.value.running == 2
    assert exc_info.value.cap == 2

    await registry.cancel_all()
    assert not h1.is_running() and not h2.is_running()


@pytest.mark.asyncio
async def test_finished_spawns_free_cap_slots():
    registry = SpawnRegistry()
    registry.max_concurrent = 1

    async def _quick() -> ToolResult:
        return ToolResult(output="ok")

    handle = registry.register(kind="delegate_claude", coro=_quick())
    await handle.task
    # Slot freed — next register succeeds.
    h2 = registry.register(kind="delegate_claude", coro=_quick())
    await h2.task
    assert h2.status() == "done"


@pytest.mark.asyncio
async def test_reserve_admits_then_register_consumes_slot():
    # M5: reserve() admits a slot atomically BEFORE the (awaited) launch, so a
    # cap check can't be bypassed by launching first and registering after.
    registry = SpawnRegistry()
    registry.max_concurrent = 1

    res = registry.reserve()  # claims the only slot
    with pytest.raises(SpawnCapExceeded):
        registry.reserve()  # cap already reserved
    with pytest.raises(SpawnCapExceeded):
        registry.register(kind="delegate_claude", coro=_never_done())

    # Converting the reservation into a real handle doesn't exceed the cap.
    handle = registry.register(kind="delegate_claude", coro=_never_done(), reservation=res)
    assert handle.is_running()
    await registry.cancel_all()


@pytest.mark.asyncio
async def test_released_reservation_frees_slot():
    # M5: a launch that fails after reserving must release the slot.
    registry = SpawnRegistry()
    registry.max_concurrent = 1
    res = registry.reserve()
    res.release()
    # Slot is free again.
    handle = registry.register(kind="delegate_claude", coro=_never_done())
    assert handle.is_running()
    await registry.cancel_all()


@pytest.mark.asyncio
async def test_reserve_respects_depth_cap():
    registry = SpawnRegistry()
    registry.depth = 2
    registry.max_depth = 2
    from tesseract.kernel.tools.base import SpawnDepthExceeded

    with pytest.raises(SpawnDepthExceeded):
        registry.reserve()


@pytest.mark.asyncio
async def test_uncapped_registry_unaffected():
    registry = SpawnRegistry()  # max_concurrent stays None
    handles = [
        registry.register(kind="delegate_claude", coro=_never_done())
        for _ in range(12)
    ]
    assert len(handles) == 12
    await registry.cancel_all()


@pytest.mark.asyncio
async def test_tool_maps_cap_to_error_result(monkeypatch):
    from tesseract.kernel.tools.delegate_tars_controller import (
        DelegateTarsControllerInput,
        DelegateTarsControllerTool,
    )

    registry = SpawnRegistry()
    registry.max_concurrent = 1
    registry.register(kind="delegate_tars_controller", coro=_never_done())

    ctx = ToolContext(workspace_root=".", session_id="parallel-tars-p4")
    ctx.spawns = registry

    result = await DelegateTarsControllerTool().run(
        DelegateTarsControllerInput(task="one too many"), ctx
    )
    assert result.is_error
    assert result.metadata["reason"] == "spawn_cap_exceeded"
    assert "spawn_await or spawn_cancel" in result.output
    await registry.cancel_all()


def test_loader_reads_repo_config_and_rejects_missing_key(tmp_path: Path):
    # Repo config carries the key.
    assert load_max_concurrent_spawns_per_session(default_runtime_config_path()) >= 1

    # Missing key raises loudly.
    stub = tmp_path / "runtime.yaml"
    stub.write_text("spawn_stall_seconds: 900\n", encoding="utf-8")
    with pytest.raises(ValueError, match="max_concurrent_spawns_per_session"):
        load_max_concurrent_spawns_per_session(stub)
