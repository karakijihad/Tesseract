"""AS-1 / MCP P1 session 2 — ActivityRecord ``goal`` + ``result`` fields.

`goal` = the intent a unit was launched with (a delegate/agent task); `result`
= terminal outcome summary. Populated for delegates: `goal` at spawn register
time (so the Mirror shows WHAT each background unit is doing, not just its kind),
`result` on the terminal transition. `parent_turn_id`/`transcript_ref` for
delegates stay unset — no turn id exists in ToolContext and a transcript path is
only mission-scoped (documented in the activity contract, not faked here).

Fakes only — nothing writes under ``tesseract/logs/``.
"""

from __future__ import annotations

import asyncio

import pytest

from tesseract.brain.spawns import SpawnRegistry, _spawn_result_summary
from tesseract.kernel.tools.base import ToolResult
from tesseract.orchestrator.activity import (
    get_activity_registry,
    reset_activity_registry,
)
from tesseract.orchestrator.activity.models import ActivityRecord, ActivityRecordOut


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_activity_registry()
    yield
    reset_activity_registry()


async def _ok() -> ToolResult:
    return ToolResult(output="line one\nline two")


async def _empty() -> ToolResult:
    return ToolResult(output="")


async def _boom() -> ToolResult:
    raise RuntimeError("nope")


def test_record_and_wire_model_carry_goal_and_result():
    r = ActivityRecord(
        activity_id="delegate:x",
        kind="delegate",
        label="delegate_claude",
        state="done",
        durability="ephemeral",
        goal="do the thing",
        result="all done",
    )
    out = ActivityRecordOut.from_record(r)
    assert out.goal == "do the thing"
    assert out.result == "all done"


async def test_spawn_registers_goal_then_sets_result_on_completion():
    reg = SpawnRegistry()
    handle = reg.register(
        kind="delegate_claude", coro=_ok(), goal="summarize the repo"
    )
    rec = get_activity_registry().get(f"delegate:{handle.handle_id}")
    assert rec.goal == "summarize the repo"
    assert rec.state == "running"
    assert rec.result is None  # not terminal yet

    await handle.task
    await asyncio.sleep(0)  # done-callback → _activity_update_spawn

    rec2 = get_activity_registry().get(f"delegate:{handle.handle_id}")
    assert rec2.state == "done"
    assert rec2.goal == "summarize the repo"  # preserved across the transition
    assert rec2.result == "line one"  # first line of output, bounded


async def test_failed_spawn_result_summary():
    reg = SpawnRegistry()
    handle = reg.register(kind="delegate_codex", coro=_boom())
    with pytest.raises(RuntimeError):
        await handle.task
    await asyncio.sleep(0)
    rec = get_activity_registry().get(f"delegate:{handle.handle_id}")
    assert rec.state == "failed"
    assert rec.result == "failed: RuntimeError"


async def test_empty_output_summary():
    reg = SpawnRegistry()
    handle = reg.register(kind="delegate_claude", coro=_empty())
    await handle.task
    await asyncio.sleep(0)
    rec = get_activity_registry().get(f"delegate:{handle.handle_id}")
    assert rec.result == "(no output)"


async def test_result_summary_none_while_running_or_cancelled():
    reg = SpawnRegistry()

    async def _slow() -> ToolResult:
        await asyncio.sleep(30)
        return ToolResult(output="never")

    handle = reg.register(kind="delegate_claude", coro=_slow())
    try:
        assert _spawn_result_summary(handle) is None  # still running
    finally:
        await reg.cancel(handle.handle_id)
    assert _spawn_result_summary(handle) is None  # cancelled → no product


async def test_spawn_label_carries_goal_snippet_not_bare_kind():
    """Task 6.1 — the Mirror renders `label` directly (ActivityMap /
    RunningSpawnsChip never read `goal`), so a bare `handle.kind` label
    left the operator seeing "delegate_claude" instead of the task."""
    reg = SpawnRegistry()
    handle = reg.register(
        kind="delegate_claude", coro=_ok(), goal="refactor the auth module"
    )
    rec = get_activity_registry().get(f"delegate:{handle.handle_id}")
    assert rec.label == "refactor the auth module"
    await handle.task
    await asyncio.sleep(0)


async def test_spawn_label_falls_back_to_kind_without_goal():
    reg = SpawnRegistry()
    handle = reg.register(kind="delegate_claude", coro=_ok())
    rec = get_activity_registry().get(f"delegate:{handle.handle_id}")
    assert rec.label == "delegate_claude"
    await handle.task
    await asyncio.sleep(0)


def test_subscriber_record_carries_goal_result():
    from tesseract.mirror.server.activity_subscriber import ActivitySubscriber

    data = {
        "activity_id": "delegate:y",
        "kind": "delegate",
        "label": "delegate_claude",
        "state": "done",
        "durability": "ephemeral",
        "goal": "g",
        "result": "r",
        "started_at": "t0",
        "updated_at": "t0",
    }
    ActivitySubscriber()._apply({"kind": "activity_registered", "data": data})
    rec = get_activity_registry().get("delegate:y")
    assert rec.goal == "g"
    assert rec.result == "r"
