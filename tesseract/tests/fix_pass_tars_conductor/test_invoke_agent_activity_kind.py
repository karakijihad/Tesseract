"""Task 6.2 — confirm invoke_agent background spawns register in the
Unified Activity Registry, so the Mirror's activity taskbar (which reads
`useActivityStore.records()` across ALL kinds) picks them up alongside
delegate_claude/codex, lane, and controller_session work.

Task 6.1's report already established that every `SpawnRegistry.register()`
call funnels through `brain/spawns.py::_spawn_record`, which hardcodes
`kind="delegate"` on the ActivityRecord regardless of the registry `kind`
string it was minted with (`invoke_agent:<name>`, `delegate_claude`, ...).
This test pins that down for invoke_agent's own kind format specifically —
no backend wiring was needed; this is a regression test, not a fix.

Fakes only — nothing writes under ``tesseract/logs/``.
"""

from __future__ import annotations

import asyncio

import pytest

from tesseract.brain.spawns import SpawnRegistry
from tesseract.kernel.tools.base import ToolResult
from tesseract.orchestrator.activity import (
    get_activity_registry,
    reset_activity_registry,
)


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    yield
    reset_activity_registry()


async def _ok() -> ToolResult:
    return ToolResult(output="ran the daily brief")


async def test_invoke_agent_spawn_registers_as_delegate_activity():
    reg = SpawnRegistry()
    handle = reg.register(
        kind="invoke_agent:daily-brief",
        coro=_ok(),
        goal="run the daily brief",
    )

    rec = get_activity_registry().get(f"delegate:{handle.handle_id}")
    assert rec is not None
    assert rec.kind == "delegate"
    assert rec.label == "run the daily brief"
    assert rec.provider is None
    assert rec.state == "running"

    await handle.task
    await asyncio.sleep(0)  # done-callback -> _activity_update_spawn

    rec2 = get_activity_registry().get(f"delegate:{handle.handle_id}")
    assert rec2.kind == "delegate"
    assert rec2.state == "done"
