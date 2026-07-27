"""End-to-end integration test for the interactive-session rail.

Drives the full TOOL layer (SessionOpen/Send/Result/Close/List) with fake
adapters for claude + codex.  No real subprocess, no real model.
Exercises parallel fan-out: both sessions open before either is collected.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.session_tools import (
    SessionCloseInput,
    SessionCloseTool,
    SessionListInput,
    SessionListTool,
    SessionOpenInput,
    SessionOpenTool,
    SessionResultInput,
    SessionResultTool,
    SessionSendInput,
    SessionSendTool,
)
from tesseract.orchestrator.tars_controller.interactive.registry import (
    InteractiveSessionRegistry,
)
from tesseract.orchestrator.tars_controller.interactive.types import SessionStatus


# ──────────────────────────── fakes ─────────────────────────────────────────


class _FakeAdapter:
    """Fake adapter — records calls, no subprocess."""

    def __init__(self, label: str = "") -> None:
        self.calls: list[str] = []
        self._label = label

    async def run_turn(self, *, task, session_id, cwd, on_event, cancel_event=None, turn_timeout=None):
        self.calls.append(task)
        text = f"reply[{self._label}]:{task}"
        on_event({"type": "assistant", "text": text})
        return _Acc(session_id or f"sid-{self._label}", text)


@dataclass
class _Acc:
    session_id: str
    result_text: str
    usage: dict = field(default_factory=dict)
    is_error: bool = False


class _FakeSpawnHandle:
    def __init__(self, handle_id: str, task: asyncio.Task) -> None:
        self.handle_id = handle_id
        self.task = task

    def is_running(self) -> bool:
        return not self.task.done()


class _FakeSpawnRegistry:
    def __init__(self) -> None:
        self._handles: dict[str, _FakeSpawnHandle] = {}
        self._counter = 0

    def register(self, *, kind: str, coro, cancel_fn=None, goal=None) -> _FakeSpawnHandle:
        # Signature tracks SpawnRegistry.register (`goal` threaded by the
        # P6 activity-label pass; stale fake broke every background test).
        self._counter += 1
        handle_id = f"spawn-{kind}-{self._counter}"
        task = asyncio.ensure_future(coro)
        sh = _FakeSpawnHandle(handle_id=handle_id, task=task)
        self._handles[handle_id] = sh
        return sh

    def get(self, handle_id: str) -> _FakeSpawnHandle | None:
        return self._handles.get(handle_id)


def _make_context(
    *,
    tmp_path,
    spawns=None,
    ask_fn=None,
    interactive_sessions=None,
    emitted: list[Any] | None = None,
) -> ToolContext:
    ctx = ToolContext(
        workspace_root=str(tmp_path), session_id="test-integration"
    )
    ctx.spawns = spawns
    ctx.ask_fn = ask_fn
    ctx.interactive_sessions = interactive_sessions or InteractiveSessionRegistry()
    if emitted is not None:
        def _emit(event: dict) -> None:
            emitted.append(event)
        ctx.session_emit = _emit
    return ctx


async def _drain_tasks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ──────────────────────────── fixtures ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _stub_cli_model(monkeypatch):
    # Keep tests hermetic — session_open resolves the CLI model from
    # roles.yaml before constructing the adapter.
    monkeypatch.setattr(
        "tesseract.kernel.tools._delegate_runner.resolve_cli_model",
        lambda role: f"{role}-test-model",
    )


@pytest.fixture
def fake_claude_adapter(monkeypatch):
    fa = _FakeAdapter("claude")
    monkeypatch.setattr(
        "tesseract.kernel.tools.session_tools.ClaudeStreamAdapter",
        lambda **kwargs: fa,
    )
    return fa


@pytest.fixture
def fake_codex_adapter(monkeypatch):
    fa = _FakeAdapter("codex")
    monkeypatch.setattr(
        "tesseract.kernel.tools.session_tools.CodexStreamAdapter",
        lambda **kwargs: fa,
    )
    return fa


# ──────────────────────────── tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_open_both_before_collect(
    tmp_path, fake_claude_adapter, fake_codex_adapter
):
    """Parallel background opens: both sessions registered before either collected."""
    spawns = _FakeSpawnRegistry()
    emitted: list[Any] = []
    ctx = _make_context(tmp_path=tmp_path, spawns=spawns, emitted=emitted)
    reg = ctx.interactive_sessions

    open_tool = SessionOpenTool()
    result_tool = SessionResultTool()

    task_text = "write a haiku"

    # Fire both opens — parallel, both before either collected.
    r_c = await open_tool.run(
        SessionOpenInput(target="claude", task=task_text, background=True), ctx
    )
    r_x = await open_tool.run(
        SessionOpenInput(target="codex", task=task_text, background=True), ctx
    )

    assert not r_c.is_error, r_c.output
    assert not r_x.is_error, r_x.output

    handle_c = r_c.metadata["handle"]
    handle_x = r_x.metadata["handle"]
    assert handle_c != handle_x

    # Both are in the registry before either is collected.
    sessions_before = reg.list()
    handles_before = {s.handle for s in sessions_before}
    assert handle_c in handles_before
    assert handle_x in handles_before

    # Running status emitted for both.
    running = [
        e for e in emitted
        if e.get("type") == "session_status" and e.get("status") == "running"
    ]
    running_handles = {e["handle"] for e in running}
    assert handle_c in running_handles
    assert handle_x in running_handles

    # Collect both results.
    await _drain_tasks()

    res_c = await result_tool.run(SessionResultInput(handle=handle_c, wait=True), ctx)
    res_x = await result_tool.run(SessionResultInput(handle=handle_x, wait=True), ctx)

    assert not res_c.is_error, res_c.output
    assert not res_x.is_error, res_x.output
    assert f"reply[claude]:{task_text}" in res_c.output
    assert f"reply[codex]:{task_text}" in res_x.output

    assert res_c.metadata["handle"] == handle_c
    assert res_x.metadata["handle"] == handle_x
    assert res_c.metadata["turn_index"] == 0
    assert res_x.metadata["turn_index"] == 0


@pytest.mark.asyncio
async def test_session_list_shows_both_open(tmp_path, fake_claude_adapter, fake_codex_adapter):
    """session_list returns both handles while they are open."""
    spawns = _FakeSpawnRegistry()
    ctx = _make_context(tmp_path=tmp_path, spawns=spawns)

    open_tool = SessionOpenTool()
    list_tool = SessionListTool()
    result_tool = SessionResultTool()

    r_c = await open_tool.run(
        SessionOpenInput(target="claude", task="task-A", background=True), ctx
    )
    r_x = await open_tool.run(
        SessionOpenInput(target="codex", task="task-B", background=True), ctx
    )
    handle_c = r_c.metadata["handle"]
    handle_x = r_x.metadata["handle"]

    # List before collecting.
    r_list = await list_tool.run(SessionListInput(), ctx)
    assert not r_list.is_error, r_list.output
    rows = json.loads(r_list.output)
    listed_handles = {r["handle"] for r in rows}
    assert handle_c in listed_handles
    assert handle_x in listed_handles

    # Drain + collect so spawns don't leak.
    await _drain_tasks()
    await result_tool.run(SessionResultInput(handle=handle_c, wait=True), ctx)
    await result_tool.run(SessionResultInput(handle=handle_x, wait=True), ctx)


@pytest.mark.asyncio
async def test_send_advances_turn_index(tmp_path, fake_claude_adapter, fake_codex_adapter):
    """session_send on a foreground-open session returns turn_index=1."""
    ctx = _make_context(tmp_path=tmp_path)

    open_tool = SessionOpenTool()
    send_tool = SessionSendTool()

    r_open = await open_tool.run(
        SessionOpenInput(target="claude", task="first turn"), ctx
    )
    assert not r_open.is_error, r_open.output
    handle = r_open.metadata["handle"]
    assert r_open.metadata["turn_index"] == 0

    r_send = await send_tool.run(
        SessionSendInput(handle=handle, message="second turn"), ctx
    )
    assert not r_send.is_error, r_send.output
    assert "reply[claude]:second turn" in r_send.output
    assert r_send.metadata["turn_index"] == 1


@pytest.mark.asyncio
async def test_send_background_turn_index_advances(
    tmp_path, fake_claude_adapter, fake_codex_adapter
):
    """Background session_send → session_result also advances turn_index."""
    spawns = _FakeSpawnRegistry()
    ctx = _make_context(tmp_path=tmp_path, spawns=spawns)

    open_tool = SessionOpenTool()
    send_tool = SessionSendTool()
    result_tool = SessionResultTool()

    r_open = await open_tool.run(
        SessionOpenInput(target="claude", task="init"), ctx
    )
    handle = r_open.metadata["handle"]
    assert r_open.metadata["turn_index"] == 0

    r_send_bg = await send_tool.run(
        SessionSendInput(handle=handle, message="background msg", background=True), ctx
    )
    assert not r_send_bg.is_error, r_send_bg.output
    assert r_send_bg.metadata["status"] == "running"

    await _drain_tasks()

    r_result = await result_tool.run(SessionResultInput(handle=handle, wait=True), ctx)
    assert not r_result.is_error, r_result.output
    assert r_result.metadata["turn_index"] == 1
    assert "reply[claude]:background msg" in r_result.output


@pytest.mark.asyncio
async def test_close_removes_from_registry(tmp_path, fake_claude_adapter, fake_codex_adapter):
    """Closing both sessions leaves registry empty."""
    spawns = _FakeSpawnRegistry()
    emitted: list[Any] = []
    ctx = _make_context(tmp_path=tmp_path, spawns=spawns, emitted=emitted)
    reg = ctx.interactive_sessions

    open_tool = SessionOpenTool()
    result_tool = SessionResultTool()
    close_tool = SessionCloseTool()
    list_tool = SessionListTool()

    r_c = await open_tool.run(
        SessionOpenInput(target="claude", task="hi", background=True), ctx
    )
    r_x = await open_tool.run(
        SessionOpenInput(target="codex", task="hi", background=True), ctx
    )
    handle_c = r_c.metadata["handle"]
    handle_x = r_x.metadata["handle"]

    await _drain_tasks()
    await result_tool.run(SessionResultInput(handle=handle_c, wait=True), ctx)
    await result_tool.run(SessionResultInput(handle=handle_x, wait=True), ctx)

    r_close_c = await close_tool.run(SessionCloseInput(handle=handle_c), ctx)
    r_close_x = await close_tool.run(SessionCloseInput(handle=handle_x), ctx)
    assert not r_close_c.is_error, r_close_c.output
    assert not r_close_x.is_error, r_close_x.output

    # Registry empty.
    assert reg.list() == []
    assert reg.get(handle_c) is None
    assert reg.get(handle_x) is None

    # List also returns empty.
    r_list = await list_tool.run(SessionListInput(), ctx)
    assert json.loads(r_list.output) == []

    # Done signals emitted.
    done_signals = [
        e for e in emitted
        if e.get("type") == "session_status" and e.get("status") == "done"
    ]
    done_handles = {e["handle"] for e in done_signals}
    assert handle_c in done_handles
    assert handle_x in done_handles


@pytest.mark.asyncio
async def test_parallel_emit_attribution(tmp_path, fake_claude_adapter, fake_codex_adapter):
    """Streamed text events carry the originating handle (parallel attribution)."""
    spawns = _FakeSpawnRegistry()
    emitted: list[Any] = []
    ctx = _make_context(tmp_path=tmp_path, spawns=spawns, emitted=emitted)

    open_tool = SessionOpenTool()
    result_tool = SessionResultTool()

    task = "parallel task"
    r_c = await open_tool.run(
        SessionOpenInput(target="claude", task=task, background=True), ctx
    )
    r_x = await open_tool.run(
        SessionOpenInput(target="codex", task=task, background=True), ctx
    )
    handle_c = r_c.metadata["handle"]
    handle_x = r_x.metadata["handle"]

    await _drain_tasks()
    await result_tool.run(SessionResultInput(handle=handle_c, wait=True), ctx)
    await result_tool.run(SessionResultInput(handle=handle_x, wait=True), ctx)

    # assistant events must carry the correct handle stamp.
    assistant_events = [e for e in emitted if e.get("type") == "assistant"]
    assert len(assistant_events) >= 2

    by_handle: dict[str, list[dict]] = {}
    for e in assistant_events:
        h = e.get("handle", "")
        by_handle.setdefault(h, []).append(e)

    # Each handle sees its own text, not the other's.
    claude_texts = " ".join(e["text"] for e in by_handle.get(handle_c, []))
    codex_texts = " ".join(e["text"] for e in by_handle.get(handle_x, []))
    assert "reply[claude]" in claude_texts
    assert "reply[codex]" in codex_texts
    # No cross-contamination.
    assert "reply[codex]" not in claude_texts
    assert "reply[claude]" not in codex_texts


@pytest.mark.asyncio
async def test_result_no_pending_returns_idle(tmp_path, fake_claude_adapter, fake_codex_adapter):
    """session_result on a session with no pending spawn returns idle metadata."""
    ctx = _make_context(tmp_path=tmp_path)

    open_tool = SessionOpenTool()
    result_tool = SessionResultTool()

    r_open = await open_tool.run(
        SessionOpenInput(target="claude", task="foreground"), ctx
    )
    handle = r_open.metadata["handle"]

    r_result = await result_tool.run(SessionResultInput(handle=handle, wait=True), ctx)
    # No background spawn was ever registered.
    assert r_result.metadata is not None
    assert r_result.metadata.get("status") == "idle"


@pytest.mark.asyncio
async def test_unknown_handle_errors(tmp_path):
    """Operations on an unknown handle return is_error=True."""
    ctx = _make_context(tmp_path=tmp_path)

    send_tool = SessionSendTool()
    result_tool = SessionResultTool()
    close_tool = SessionCloseTool()

    r_send = await send_tool.run(
        SessionSendInput(handle="ghost-handle", message="hi"), ctx
    )
    assert r_send.is_error
    assert "ghost-handle" in r_send.output

    r_result = await result_tool.run(
        SessionResultInput(handle="ghost-handle"), ctx
    )
    assert r_result.is_error

    # session_close is idempotent — unknown handles return ok (not an error).
    r_close = await close_tool.run(SessionCloseInput(handle="ghost-handle"), ctx)
    assert not r_close.is_error
