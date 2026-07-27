"""parallel-tars P1 — lane_turn fire-and-track background mode.

Exercises the background-by-default contract against a scripted fake
LaneManager: default input returns a spawn_handle immediately and the
registered task resolves to the same ToolResult the foreground path
produces; background=false keeps the inline semantics; a context with
no SpawnRegistry degrades to foreground instead of erroring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.lane_turn import LaneTurnInput, LaneTurnTool


@dataclass
class _FakeEvent:
    kind: str
    payload: dict[str, Any]
    cursor: int


@dataclass
class _FakeSendResult:
    accepted: bool = True
    reason: str | None = None
    queue_depth: int = 0


@dataclass
class _FakeManager:
    """Scripted lane: first read returns the pre-send tail, the read after
    send returns one assistant_text + turn_ended."""

    accept: bool = True
    reads: int = 0
    sent: list[tuple[str, str]] = field(default_factory=list)

    def read(self, lane_id: str, cursor: int | None):
        self.reads += 1
        if self.reads == 1:
            return [], 0
        events = [
            _FakeEvent("assistant_text", {"text": "lane reply"}, 1),
            _FakeEvent("turn_ended", {"is_error": False}, 2),
        ]
        return events, 2

    async def send(self, lane_id: str, message: str):
        self.sent.append((lane_id, message))
        return _FakeSendResult(accepted=self.accept)


def _make_ctx(manager: _FakeManager, registry: SpawnRegistry | None) -> ToolContext:
    ctx = ToolContext(workspace_root=".", session_id="parallel-tars-p1")
    ctx.lane_manager_provider = lambda: manager
    ctx.spawns = registry
    return ctx


@pytest.fixture(autouse=True)
def _fast_relay(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "tesseract.kernel.tools.lane_turn.load_conductor_relay",
        lambda: (0.01, 5.0),
    )
    monkeypatch.setattr(
        "tesseract.kernel.tools.lane_turn.load_conductor_reply_cap",
        lambda: 10_000,
    )


@pytest.mark.asyncio
async def test_default_background_returns_handle_and_task_yields_reply():
    manager = _FakeManager()
    registry = SpawnRegistry()
    tool = LaneTurnTool()

    result = await tool.run(
        LaneTurnInput(name_or_id="coder/claude", message="do the thing"),
        _make_ctx(manager, registry),
    )

    assert not result.is_error
    handle_id = result.metadata["spawn_handle"]
    assert result.metadata["spawn_kind"] == "lane_turn:coder/claude"
    assert result.metadata["status"] == "running"
    assert handle_id in result.output

    handle = registry.get(handle_id)
    assert handle is not None
    inner = await handle.task
    assert isinstance(inner, ToolResult)
    assert not inner.is_error
    assert "lane reply" in inner.output
    assert inner.metadata["turn_completed"] is True
    assert manager.sent == [("coder/claude", "do the thing")]


@pytest.mark.asyncio
async def test_background_false_runs_inline():
    manager = _FakeManager()
    tool = LaneTurnTool()

    result = await tool.run(
        LaneTurnInput(name_or_id="lane-1", message="hi", background=False),
        _make_ctx(manager, SpawnRegistry()),
    )

    assert not result.is_error
    assert "spawn_handle" not in result.metadata
    assert "lane reply" in result.output
    assert result.metadata["turn_completed"] is True


@pytest.mark.asyncio
async def test_no_registry_degrades_to_foreground():
    manager = _FakeManager()
    tool = LaneTurnTool()

    result = await tool.run(
        LaneTurnInput(name_or_id="lane-1", message="hi"),
        _make_ctx(manager, registry=None),
    )

    assert not result.is_error
    assert "spawn_handle" not in result.metadata
    assert "lane reply" in result.output


@pytest.mark.asyncio
async def test_background_send_rejection_surfaces_as_error_result():
    manager = _FakeManager(accept=False)
    registry = SpawnRegistry()
    tool = LaneTurnTool()

    result = await tool.run(
        LaneTurnInput(name_or_id="lane-1", message="hi"),
        _make_ctx(manager, registry),
    )
    handle = registry.get(result.metadata["spawn_handle"])
    inner = await handle.task
    assert inner.is_error
    assert "rejected" in inner.output
