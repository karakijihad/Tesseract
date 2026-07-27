"""parallel-tars P2 — delegate_tars_controller fire-and-track background mode.

Monkeypatches `dispatch_to_controller` (imported into the tool module's
namespace) so no daemon is needed: default input returns a spawn_handle
and the spawn task resolves to the mapped ToolResult; background=false
keeps inline semantics; timeout maps to is_error+timed_out inside the
spawn; a registry-less context degrades to foreground.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.delegate_tars_controller import (
    DelegateTarsControllerInput,
    DelegateTarsControllerTool,
)


@dataclass
class _FakeDispatchResult:
    session_id: str = "ctrl-sess-1"
    assistant_text: str = "controller reply"
    saw_assistant_text: bool = True
    timed_out: bool = False
    cancelled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def _patch_dispatch(monkeypatch: pytest.MonkeyPatch, result: _FakeDispatchResult):
    calls: list[dict[str, Any]] = []

    async def _fake(prompt: str, **kwargs: Any) -> _FakeDispatchResult:
        calls.append({"prompt": prompt, **kwargs})
        return result

    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_tars_controller.dispatch_to_controller",
        _fake,
    )
    return calls


def _make_ctx(registry: SpawnRegistry | None) -> ToolContext:
    ctx = ToolContext(workspace_root=".", session_id="parallel-tars-p2")
    ctx.spawns = registry
    return ctx


@pytest.mark.asyncio
async def test_default_background_returns_handle_and_task_yields_reply(monkeypatch):
    calls = _patch_dispatch(monkeypatch, _FakeDispatchResult())
    registry = SpawnRegistry()
    tool = DelegateTarsControllerTool()

    result = await tool.run(
        DelegateTarsControllerInput(task="fix the widget"), _make_ctx(registry)
    )

    assert not result.is_error
    assert result.metadata["spawn_kind"] == "delegate_tars_controller"
    handle = registry.get(result.metadata["spawn_handle"])
    assert handle is not None

    inner = await handle.task
    assert isinstance(inner, ToolResult)
    assert not inner.is_error
    assert inner.output == "controller reply"
    assert inner.metadata["session_id"] == "ctrl-sess-1"
    # The spawn coroutine still tails to completion — wait_for_completion=True.
    assert calls[0]["wait_for_completion"] is True


@pytest.mark.asyncio
async def test_background_false_runs_inline(monkeypatch):
    _patch_dispatch(monkeypatch, _FakeDispatchResult())
    tool = DelegateTarsControllerTool()

    result = await tool.run(
        DelegateTarsControllerInput(task="fix", background=False),
        _make_ctx(SpawnRegistry()),
    )
    assert not result.is_error
    assert "spawn_handle" not in result.metadata
    assert result.output == "controller reply"


@pytest.mark.asyncio
async def test_background_timeout_maps_to_error_in_spawn(monkeypatch):
    _patch_dispatch(
        monkeypatch,
        _FakeDispatchResult(assistant_text="", saw_assistant_text=False, timed_out=True),
    )
    registry = SpawnRegistry()
    tool = DelegateTarsControllerTool()

    result = await tool.run(DelegateTarsControllerInput(task="slow"), _make_ctx(registry))
    inner = await registry.get(result.metadata["spawn_handle"]).task
    assert inner.is_error
    assert inner.timed_out
    assert inner.metadata["timed_out"] is True


@pytest.mark.asyncio
async def test_no_registry_degrades_to_foreground(monkeypatch):
    _patch_dispatch(monkeypatch, _FakeDispatchResult())
    tool = DelegateTarsControllerTool()

    result = await tool.run(
        DelegateTarsControllerInput(task="fix"), _make_ctx(registry=None)
    )
    assert not result.is_error
    assert "spawn_handle" not in result.metadata
    assert result.output == "controller reply"


@pytest.mark.asyncio
async def test_background_spawn_gets_detached_cancel_event(monkeypatch):
    """Reviewer finding 2026-07-09: a detached spawn must not inherit the
    session-lifetime cancel_event — Stop on a later, unrelated turn would
    kill it. spawn_cancel still reaches the detached event via cancel_fn."""
    calls = _patch_dispatch(monkeypatch, _FakeDispatchResult())
    registry = SpawnRegistry()
    tool = DelegateTarsControllerTool()
    ctx = _make_ctx(registry)

    result = await tool.run(DelegateTarsControllerInput(task="long job"), ctx)
    handle = registry.get(result.metadata["spawn_handle"])
    await handle.task

    spawn_event = calls[0]["cancel_event"]
    assert spawn_event is not ctx.cancel_event
    ctx.cancel_event.set()  # a later turn's Stop button
    assert not spawn_event.is_set()
    assert handle.cancel_fn is not None
    handle.cancel_fn()  # spawn_cancel's explicit path
    assert spawn_event.is_set()
