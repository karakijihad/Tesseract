"""Tests for session_open / session_send / session_result / session_close / session_list tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.session_tools import (
    SessionCloseTool,
    SessionListTool,
    SessionOpenInput,
    SessionOpenTool,
    SessionResultTool,
    SessionSendTool,
)
from tesseract.orchestrator.tars_controller.interactive.registry import (
    InteractiveSessionRegistry,
)
from tesseract.orchestrator.tars_controller.interactive.types import (
    SessionStatus,
    TurnResult,
)


# ──────────────────────────── fakes ─────────────────────────────────────────


class _FakeAdapter:
    """Fake ClaudeStreamAdapter / CodexStreamAdapter — no subprocess."""

    async def run_turn(self, *, task, session_id, cwd, on_event, cancel_event=None, turn_timeout=None):
        on_event({"type": "assistant", "text": f"reply:{task}"})
        return _Acc(session_id or "sid-1", f"reply:{task}")


@dataclass
class _Acc:
    session_id: str
    result_text: str
    usage: dict = field(default_factory=dict)
    is_error: bool = False


class _FakeChatSession:
    async def send(self, text: str):
        from tesseract.kernel.adapters.base import ChunkType, StreamChunk

        yield StreamChunk(type=ChunkType.TEXT, text=f"agent:{text}")
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")


class _FakeSpawnHandle:
    def __init__(self, handle_id: str, task: asyncio.Task) -> None:
        self.handle_id = handle_id
        self.task = task

    def is_running(self) -> bool:
        return not self.task.done()


class _FakeSpawnRegistry:
    """Minimal SpawnRegistry shim — registers coros as real asyncio Tasks."""

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
) -> ToolContext:
    ctx = ToolContext(workspace_root=str(tmp_path), session_id="test-session-tools")
    ctx.spawns = spawns
    ctx.ask_fn = ask_fn
    ctx.interactive_sessions = interactive_sessions or InteractiveSessionRegistry()
    return ctx


# ──────────────────────────── fixtures ──────────────────────────────────────


@pytest.fixture
def fake_adapter(monkeypatch):
    """Patch ClaudeStreamAdapter and CodexStreamAdapter to return _FakeAdapter."""
    fa = _FakeAdapter()
    monkeypatch.setattr(
        "tesseract.kernel.tools.session_tools.ClaudeStreamAdapter",
        lambda **kwargs: fa,
    )
    monkeypatch.setattr(
        "tesseract.kernel.tools.session_tools.CodexStreamAdapter",
        lambda **kwargs: fa,
    )
    # Keep tests hermetic — session_open resolves the CLI model from
    # roles.yaml before constructing the adapter.
    monkeypatch.setattr(
        "tesseract.kernel.tools._delegate_runner.resolve_cli_model",
        lambda role: f"{role}-test-model",
    )
    return fa


@pytest.fixture
def fake_build_agent(monkeypatch):
    """Patch build_agent_session to return a _FakeChatSession."""
    session = _FakeChatSession()
    monkeypatch.setattr(
        "tesseract.kernel.tools.session_tools.build_agent_session",
        lambda **_kwargs: session,
    )
    return session


# ──────────────────────────── tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_claude_no_ask(tmp_path, monkeypatch, fake_adapter):
    """CLI backend opens without ask_fn; handle lands in registry; result returned."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _make_context(tmp_path=tmp_path)
    tool = SessionOpenTool()

    result = await tool.run(SessionOpenInput(target="claude", task="hello"), ctx)

    assert not result.is_error, result.output
    assert "reply:hello" in result.output
    assert result.metadata is not None
    handle = result.metadata["handle"]
    assert ctx.interactive_sessions.get(handle) is not None


@pytest.mark.asyncio
async def test_open_codex_no_ask(tmp_path, monkeypatch, fake_adapter):
    """Codex is also a CLI backend — no ask_fn needed."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _make_context(tmp_path=tmp_path)
    tool = SessionOpenTool()

    result = await tool.run(SessionOpenInput(target="codex", task="do x"), ctx)

    assert not result.is_error, result.output
    handle = result.metadata["handle"]
    assert ctx.interactive_sessions.get(handle) is not None


@pytest.mark.asyncio
async def test_cli_open_is_deliberate_auto_exception(tmp_path, monkeypatch, fake_adapter):
    """Codex audit 2026-05-25 M-2 — DELIBERATE security decision (operator
    confirmed 2026-05-25, recorded in Docs/Doclog/2026-05-25.md).

    The claude/codex CLI backends launch full-access subprocesses
    (`--dangerously-skip-permissions` / `--dangerously-bypass-approvals-and-sandbox`),
    moving execution outside TESSERACT's bash-security + permissions pipeline.
    The operator chose to KEEP this path at AUTO posture — the trust boundary
    is "do you trust the claude/codex binary", not a per-call ASK. This test
    PINS that decision: the CLI open path must NOT consult ask_fn even when one
    is wired. Flipping it to ASK is a conscious change that breaks this test.

    Agent-backend targets remain ASK-gated (see test_open_agent_* below) — the
    exception is scoped to the operator's own claude/codex subscription CLIs.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ask_calls: list[str] = []

    async def _recording_ask(_tool, _inp, _ctx):
        ask_calls.append(getattr(_inp, "target", "?"))
        return True

    assert SessionOpenTool.default_posture == "auto"

    for target in ("claude", "codex"):
        ctx = _make_context(tmp_path=tmp_path, ask_fn=_recording_ask)
        result = await SessionOpenTool().run(
            SessionOpenInput(target=target, task="hi"), ctx
        )
        assert not result.is_error, result.output

    assert ask_calls == [], f"CLI open unexpectedly consulted ask_fn: {ask_calls}"


@pytest.mark.asyncio
async def test_open_agent_requires_ask_denied(tmp_path, monkeypatch):
    """Agent backend: ask_fn returns False → denied ToolResult; not in registry."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    async def _deny(_tool, _inp, _ctx):
        return False

    ctx = _make_context(tmp_path=tmp_path, ask_fn=_deny)
    tool = SessionOpenTool()

    result = await tool.run(
        SessionOpenInput(target="my-agent", task="do something"), ctx
    )

    assert result.denied_hard is True
    assert result.deny_reason
    # Registry should be empty — session was not added
    assert ctx.interactive_sessions.list() == []


@pytest.mark.asyncio
async def test_open_agent_no_ask_fn_hard_deny(tmp_path, monkeypatch):
    """Agent backend with no ask_fn → hard deny (no approval channel)."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _make_context(tmp_path=tmp_path, ask_fn=None)
    tool = SessionOpenTool()

    result = await tool.run(
        SessionOpenInput(target="some-agent", task="task"), ctx
    )

    assert result.is_error
    assert result.denied_hard
    assert ctx.interactive_sessions.list() == []


@pytest.mark.asyncio
async def test_open_agent_ask_granted_builds(tmp_path, monkeypatch, fake_build_agent):
    """Agent backend: ask_fn→True; build_agent_session called; session added."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    async def _approve(_tool, _inp, _ctx):
        return True

    ctx = _make_context(tmp_path=tmp_path, ask_fn=_approve)
    tool = SessionOpenTool()

    result = await tool.run(
        SessionOpenInput(target="my-agent", task="hello agent"), ctx
    )

    assert not result.is_error, result.output
    handle = result.metadata["handle"]
    assert ctx.interactive_sessions.get(handle) is not None
    assert "agent:hello agent" in result.output


@pytest.mark.asyncio
async def test_send_unknown_handle_errors(tmp_path, monkeypatch):
    """session_send with an unknown handle returns is_error=True."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _make_context(tmp_path=tmp_path)
    tool = SessionSendTool()

    from tesseract.kernel.tools.session_tools import SessionSendInput

    result = await tool.run(
        SessionSendInput(handle="nonexistent-handle", message="hi"), ctx
    )
    assert result.is_error
    assert "nonexistent-handle" in result.output


@pytest.mark.asyncio
async def test_send_foreground(tmp_path, monkeypatch, fake_adapter):
    """session_send forwards message to open session and returns result."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _make_context(tmp_path=tmp_path)
    open_tool = SessionOpenTool()
    send_tool = SessionSendTool()

    from tesseract.kernel.tools.session_tools import SessionSendInput

    r_open = await open_tool.run(SessionOpenInput(target="claude", task="start"), ctx)
    assert not r_open.is_error
    handle = r_open.metadata["handle"]

    r_send = await send_tool.run(SessionSendInput(handle=handle, message="follow-up"), ctx)
    assert not r_send.is_error
    assert "reply:follow-up" in r_send.output


@pytest.mark.asyncio
async def test_close_removes_from_registry(tmp_path, monkeypatch, fake_adapter):
    """session_close removes the session from the registry; idempotent on re-call."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _make_context(tmp_path=tmp_path)
    open_tool = SessionOpenTool()
    close_tool = SessionCloseTool()

    from tesseract.kernel.tools.session_tools import SessionCloseInput

    r_open = await open_tool.run(SessionOpenInput(target="claude", task="hello"), ctx)
    handle = r_open.metadata["handle"]
    assert ctx.interactive_sessions.get(handle) is not None

    r_close = await close_tool.run(SessionCloseInput(handle=handle), ctx)
    assert not r_close.is_error
    assert ctx.interactive_sessions.get(handle) is None

    # Second close is idempotent
    r_close2 = await close_tool.run(SessionCloseInput(handle=handle), ctx)
    assert not r_close2.is_error


@pytest.mark.asyncio
async def test_list_returns_handles(tmp_path, monkeypatch, fake_adapter):
    """session_list enumerates all open sessions."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _make_context(tmp_path=tmp_path)
    open_tool = SessionOpenTool()
    list_tool = SessionListTool()

    from tesseract.kernel.tools.session_tools import SessionListInput

    r_list_empty = await list_tool.run(SessionListInput(), ctx)
    import json

    assert json.loads(r_list_empty.output) == []

    await open_tool.run(SessionOpenInput(target="claude", task="t1"), ctx)
    await open_tool.run(SessionOpenInput(target="codex", task="t2"), ctx)

    r_list = await list_tool.run(SessionListInput(), ctx)
    rows = json.loads(r_list.output)
    assert len(rows) == 2
    targets = {r["target"] for r in rows}
    assert targets == {"claude", "codex"}


@pytest.mark.asyncio
async def test_background_open_then_result(tmp_path, monkeypatch, fake_adapter):
    """background=True: open registers spawn + stores _pending_spawn_id; session_result collects."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    spawns = _FakeSpawnRegistry()
    ctx = _make_context(tmp_path=tmp_path, spawns=spawns)
    open_tool = SessionOpenTool()
    result_tool = SessionResultTool()

    from tesseract.kernel.tools.session_tools import SessionResultInput

    r_open = await open_tool.run(
        SessionOpenInput(target="claude", task="bg-task", background=True), ctx
    )

    assert not r_open.is_error, r_open.output
    assert r_open.metadata is not None
    handle = r_open.metadata["handle"]
    spawn_handle_id = r_open.metadata["spawn_handle"]
    assert spawn_handle_id is not None

    # Session object should have _pending_spawn_id set
    session = ctx.interactive_sessions.get(handle)
    assert session is not None
    assert getattr(session, "_pending_spawn_id", None) == spawn_handle_id

    # Collect the result
    r_result = await result_tool.run(
        SessionResultInput(handle=handle, wait=True), ctx
    )

    assert not r_result.is_error, r_result.output
    assert "reply:bg-task" in r_result.output

    # _pending_spawn_id cleared after collection
    assert getattr(session, "_pending_spawn_id", None) is None


@pytest.mark.asyncio
async def test_result_no_pending_spawn(tmp_path, monkeypatch, fake_adapter):
    """session_result when no background turn is pending returns idle status."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _make_context(tmp_path=tmp_path)
    open_tool = SessionOpenTool()
    result_tool = SessionResultTool()

    from tesseract.kernel.tools.session_tools import SessionResultInput

    # Foreground open — no spawn registered
    r_open = await open_tool.run(SessionOpenInput(target="claude", task="hello"), ctx)
    handle = r_open.metadata["handle"]

    r_result = await result_tool.run(SessionResultInput(handle=handle, wait=True), ctx)
    # Not an error — just idle
    assert not r_result.is_error
    assert "idle" in (r_result.metadata or {}).get("status", "") or "no pending" in r_result.output


@pytest.mark.asyncio
async def test_open_no_interactive_sessions_wired(tmp_path, monkeypatch, fake_adapter):
    """session_open with no interactive_sessions in context returns error."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    ctx = _make_context(tmp_path=tmp_path)
    ctx.interactive_sessions = None  # unwired
    tool = SessionOpenTool()

    result = await tool.run(SessionOpenInput(target="claude", task="test"), ctx)
    assert result.is_error
    assert "not wired" in result.output


@pytest.mark.asyncio
async def test_background_open_emits_running_worker_status(tmp_path, monkeypatch, fake_adapter):
    """background=True: session_open emits WorkerStatusEvent(status='running') via session_emit.

    Previously the background path returned bg_result without emitting the lifecycle
    marker, so the TUI rail row never appeared for background-opened sessions.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.orchestrator.tars_controller.events import WorkerStatusEvent

    emitted: list[Any] = []

    def _fake_session_emit(event: dict) -> None:
        emitted.append(event)

    spawns = _FakeSpawnRegistry()
    ctx = _make_context(tmp_path=tmp_path, spawns=spawns)
    ctx.session_emit = _fake_session_emit  # wire the session_emit bridge

    tool = SessionOpenTool()
    r_open = await tool.run(
        SessionOpenInput(target="claude", task="bg-emit-test", background=True), ctx
    )

    assert not r_open.is_error, r_open.output
    handle = (r_open.metadata or {}).get("handle")
    assert handle is not None

    # session_emit must have received a session_status running event.
    running_signals = [
        e for e in emitted
        if e.get("type") == "session_status" and e.get("status") == "running"
    ]
    assert len(running_signals) == 1
    assert running_signals[0]["handle"] == handle
    assert running_signals[0]["target"] == "claude"


# ─── Fix 1: foreground open failure must not leave a broken session ──────────


@pytest.mark.asyncio
async def test_foreground_open_error_not_registered(tmp_path, monkeypatch):
    """A CLI backend whose open() fails must NOT leave the session in the registry."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    class _ErrorAdapter:
        async def run_turn(self, *, task, session_id, cwd, on_event, cancel_event=None, turn_timeout=None):
            # Return an accumulator that signals error
            return _Acc(session_id or "sid-err", "backend failure", is_error=True)

    fa = _ErrorAdapter()
    monkeypatch.setattr("tesseract.kernel.tools.session_tools.ClaudeStreamAdapter", lambda **kwargs: fa)
    monkeypatch.setattr("tesseract.kernel.tools.session_tools.CodexStreamAdapter", lambda **kwargs: fa)
    monkeypatch.setattr(
        "tesseract.kernel.tools._delegate_runner.resolve_cli_model",
        lambda role: f"{role}-test-model",
    )

    ctx = _make_context(tmp_path=tmp_path)
    tool = SessionOpenTool()

    result = await tool.run(SessionOpenInput(target="claude", task="fail-me", background=False), ctx)

    assert result.is_error, "expected error ToolResult from failing backend"
    # Registry must be empty — broken session must not remain registered
    assert ctx.interactive_sessions.list() == [], "session must be removed from registry on failed open"


# ─── Fix 3: second background turn must be refused until result collected ─────


@pytest.mark.asyncio
async def test_double_background_refused(tmp_path, monkeypatch, fake_adapter):
    """A second background send before collecting the first must return is_error."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    spawns = _FakeSpawnRegistry()
    ctx = _make_context(tmp_path=tmp_path, spawns=spawns)

    open_tool = SessionOpenTool()
    send_tool = SessionSendTool()
    result_tool = SessionResultTool()

    from tesseract.kernel.tools.session_tools import SessionResultInput, SessionSendInput

    # Open foreground so we have a clean session with no pending spawn
    r_open = await open_tool.run(SessionOpenInput(target="claude", task="initial"), ctx)
    assert not r_open.is_error
    handle = r_open.metadata["handle"]

    # First background send — should succeed and park a _pending_spawn_id
    r_bg1 = await send_tool.run(SessionSendInput(handle=handle, message="bg-msg-1", background=True), ctx)
    assert not r_bg1.is_error, r_bg1.output
    assert r_bg1.metadata["status"] == "running"

    # Second background send BEFORE collecting — must be refused
    r_bg2 = await send_tool.run(SessionSendInput(handle=handle, message="bg-msg-2", background=True), ctx)
    assert r_bg2.is_error, "double-background send must be refused"
    assert "already in flight" in r_bg2.output

    # Collect the first background turn
    r_result = await result_tool.run(SessionResultInput(handle=handle, wait=True), ctx)
    assert not r_result.is_error, r_result.output

    # Pending is now cleared — a subsequent background send must be accepted
    r_bg3 = await send_tool.run(SessionSendInput(handle=handle, message="bg-msg-3", background=True), ctx)
    assert not r_bg3.is_error, f"third background send (after collect) must succeed: {r_bg3.output}"


# ─── Fix 1 (resource): session_close must cancel in-flight background spawn ──


class _FakeSpawnRegistryWithCancel(_FakeSpawnRegistry):
    """Extends _FakeSpawnRegistry with a real async cancel method."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled_ids: list[str] = []

    async def cancel(self, handle_id: str) -> bool:
        h = self._handles.get(handle_id)
        if h is None:
            return False
        if h.task.done():
            return False
        self.cancelled_ids.append(handle_id)
        h.task.cancel()
        try:
            await h.task
        except (asyncio.CancelledError, Exception):
            pass
        return True


@pytest.mark.asyncio
async def test_close_cancels_pending_background_spawn(tmp_path, monkeypatch, fake_adapter):
    """session_close must cancel the in-flight background spawn before removing the handle.

    Verifies Fix 1: if _pending_spawn_id is set on the session object,
    SessionCloseTool.run must call spawns.cancel(spawn_id) before reg.remove.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    spawns = _FakeSpawnRegistryWithCancel()
    ctx = _make_context(tmp_path=tmp_path, spawns=spawns)

    open_tool = SessionOpenTool()
    close_tool = SessionCloseTool()

    from tesseract.kernel.tools.session_tools import SessionCloseInput

    # Open a background turn so _pending_spawn_id is set
    r_open = await open_tool.run(
        SessionOpenInput(target="claude", task="bg-close-test", background=True), ctx
    )
    assert not r_open.is_error, r_open.output
    handle = r_open.metadata["handle"]
    spawn_handle_id = r_open.metadata["spawn_handle"]

    session = ctx.interactive_sessions.get(handle)
    assert session is not None
    assert getattr(session, "_pending_spawn_id", None) == spawn_handle_id

    # Close the session — must cancel the spawn
    r_close = await close_tool.run(SessionCloseInput(handle=handle), ctx)
    assert not r_close.is_error

    # Handle removed from registry
    assert ctx.interactive_sessions.get(handle) is None

    # spawn.cancel was called for the correct spawn id
    assert spawn_handle_id in spawns.cancelled_ids, (
        f"expected cancel({spawn_handle_id!r}); got {spawns.cancelled_ids}"
    )

    # _pending_spawn_id cleared on the session object
    assert getattr(session, "_pending_spawn_id", None) is None
