"""W3 — `start_controller_session` idle-wake fix (Deferred §P6): a detached
controller session registers a SpawnRegistry tail so its completion wakes
TARS, same as `delegate_tars_controller` background."""

from __future__ import annotations

import asyncio

import pytest

from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.start_controller_session import (
    StartControllerSessionInput,
    StartControllerSessionTool,
)
from tesseract.orchestrator.tars_controller.dispatcher import DispatchResult


def _patch_dispatch(monkeypatch, session_id="2026-07-10-abcdef01"):
    from tesseract.kernel.tools import start_controller_session as mod

    async def _fake_dispatch(**kwargs):
        assert kwargs["wait_for_completion"] is False
        return DispatchResult(session_id=session_id, metadata={"detached": True})

    monkeypatch.setattr(mod, "dispatch_to_controller", _fake_dispatch)
    return session_id


@pytest.mark.asyncio
async def test_detached_session_registers_wake_tail(monkeypatch):
    session_id = _patch_dispatch(monkeypatch)

    from tesseract.orchestrator.tars_controller import dispatcher

    async def _fake_reattach(sid, *, cancel_event=None, **kwargs):
        return DispatchResult(
            session_id=sid,
            assistant_text="controller finished the job",
            saw_assistant_text=True,
            metadata={"reattached": True},
        )

    monkeypatch.setattr(dispatcher, "reattach_to_controller", _fake_reattach)

    registry = SpawnRegistry()
    ctx = ToolContext(workspace_root=".", session_id="trio-w3")
    ctx.spawns = registry

    result = await StartControllerSessionTool().run(
        StartControllerSessionInput(task="do the heavy thing"), ctx
    )
    assert not result.is_error
    handle_id = result.metadata["spawn_handle"]
    handle = registry.get(handle_id)
    assert handle is not None
    assert handle.kind == f"controller_session:{session_id}"

    tail_result = await handle.task
    assert "controller finished the job" in tail_result.output
    assert handle.status() == "done"


@pytest.mark.asyncio
async def test_cap_hit_rejects_launch_before_dispatch(monkeypatch):
    # M5: a cap hit must REJECT the launch before dispatching — the old
    # behavior launched an untracked controller and only skipped the wake tail.
    dispatched = {"called": False}

    async def _spy_dispatch(**kwargs):
        dispatched["called"] = True
        return DispatchResult(session_id="2026-07-10-abcdef01", metadata={})

    from tesseract.kernel.tools import start_controller_session as mod

    monkeypatch.setattr(mod, "dispatch_to_controller", _spy_dispatch)

    async def _never() -> ToolResult:
        await asyncio.Event().wait()
        return ToolResult(output="unreachable")

    registry = SpawnRegistry()
    registry.max_concurrent = 1
    registry.register(kind="delegate_claude", coro=_never())

    ctx = ToolContext(workspace_root=".", session_id="trio-w3")
    ctx.spawns = registry

    result = await StartControllerSessionTool().run(
        StartControllerSessionInput(task="another heavy thing"), ctx
    )
    assert result.is_error
    assert result.metadata["reason"] == "spawn_cap_exceeded"
    assert dispatched["called"] is False  # no untracked launch
    await registry.cancel_all()


@pytest.mark.asyncio
async def test_no_registry_degrades_silently(monkeypatch):
    """REPL/headless contexts carry no SpawnRegistry — behavior unchanged."""
    _patch_dispatch(monkeypatch)
    result = await StartControllerSessionTool().run(
        StartControllerSessionInput(task="headless dispatch"),
        ToolContext(workspace_root=".", session_id=""),
    )
    assert not result.is_error
    assert "spawn_handle" not in (result.metadata or {})


@pytest.mark.asyncio
async def test_tail_failure_is_error_result_not_crash(monkeypatch):
    session_id = _patch_dispatch(monkeypatch)

    from tesseract.orchestrator.tars_controller import dispatcher
    from tesseract.orchestrator.tars_controller.dispatcher import DispatcherError

    async def _fail_reattach(sid, *, cancel_event=None, **kwargs):
        raise DispatcherError("daemon went away")

    monkeypatch.setattr(dispatcher, "reattach_to_controller", _fail_reattach)

    registry = SpawnRegistry()
    ctx = ToolContext(workspace_root=".", session_id="trio-w3")
    ctx.spawns = registry

    result = await StartControllerSessionTool().run(
        StartControllerSessionInput(task="tail will fail"), ctx
    )
    handle = registry.get(result.metadata["spawn_handle"])
    tail_result = await handle.task
    assert tail_result.is_error
    assert "daemon went away" in tail_result.output
    assert handle.status() == "done"  # error RESULT, not a raised exception
