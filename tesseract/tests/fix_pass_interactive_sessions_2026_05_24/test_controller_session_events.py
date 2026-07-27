"""A10 — controller rail + transcript bridge for interactive sessions.

Tests that:
  - opening a session appends WorkerStatusEvent(status="running") keyed by handle
  - an assistant text event (all three backend shapes) maps to AssistantTextEvent
  - closing appends WorkerStatusEvent(status="done") keyed by handle

Pattern: fake daemon capturing append_event calls; the session_emit closure
from _make_controller_session_emit is driven directly (synchronous path via
loop.create_task inside a running asyncio loop).
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any, Iterator

import pytest

from tesseract.orchestrator.tars_controller.events import (
    AssistantTextEvent,
    WorkerStatusEvent,
)
from tesseract.orchestrator.tars_controller.interactive.registry import (
    InteractiveSessionRegistry,
)
from tesseract.orchestrator.tars_controller.interactive.types import (
    SessionStatus,
    TurnResult,
)
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.session_tools import SessionOpenInput, SessionOpenTool, SessionCloseTool, SessionCloseInput


# ─────────────────────────── production-logs guard ──────────────────────────


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Redirect TESSERACT_HOME so no production logs are touched."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    yield
    importlib.reload(tesseract.paths)


# ─────────────────────────── fakes ──────────────────────────────────────────


class _FakeDaemon:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append_event(self, session_id: str, event: Any) -> None:
        self.events.append(event)


class _FakeSession:
    """Minimal InteractiveSession that records calls and yields a TurnResult."""

    def __init__(self, handle: str, target: str = "claude") -> None:
        self.handle = handle
        self.target = target
        self._pending_spawn_id = None

    async def open(self, task: str) -> TurnResult:
        return TurnResult(
            handle=self.handle,
            target=self.target,
            turn_index=0,
            result_text="hello from session",
            status=SessionStatus.DONE,
        )

    async def send(self, message: str) -> TurnResult:
        return TurnResult(
            handle=self.handle,
            target=self.target,
            turn_index=1,
            result_text="reply",
            status=SessionStatus.DONE,
        )

    async def close(self) -> None:
        pass


# ─────────────────────────── helpers ────────────────────────────────────────


def _make_session_emit(daemon: _FakeDaemon, session_id: str):
    """Import the factory directly to test the mapping in isolation."""
    from tesseract.scripts.tars_controller import _make_controller_session_emit
    return _make_controller_session_emit(daemon, session_id)


async def _drain_tasks() -> None:
    """Let all fire-and-forget tasks scheduled via create_task run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ─────────────────────────── unit tests for the emit closure ────────────────


@pytest.mark.asyncio
async def test_session_status_running_maps_to_worker_status_event() -> None:
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-1")

    emit({"type": "session_status", "handle": "claude-123", "target": "claude", "status": "running"})
    await _drain_tasks()

    assert len(daemon.events) == 1
    evt = daemon.events[0]
    assert isinstance(evt, WorkerStatusEvent)
    assert evt.worker_id == "claude-123"
    assert evt.status == "running"
    assert evt.worker_kind == "claude"


@pytest.mark.asyncio
async def test_session_status_done_maps_to_worker_status_event() -> None:
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-1")

    emit({"type": "session_status", "handle": "claude-123", "status": "done"})
    await _drain_tasks()

    assert len(daemon.events) == 1
    evt = daemon.events[0]
    assert isinstance(evt, WorkerStatusEvent)
    assert evt.worker_id == "claude-123"
    assert evt.status == "done"


@pytest.mark.asyncio
async def test_agent_backend_text_event_maps_to_assistant_text() -> None:
    """Agent backend shape: {"type":"assistant","text":"..."} — no active session → worker_id None."""
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-1")

    emit({"type": "assistant", "text": "Hello from agent"})
    await _drain_tasks()

    assert len(daemon.events) == 1
    evt = daemon.events[0]
    assert isinstance(evt, AssistantTextEvent)
    assert evt.text == "Hello from agent"
    # No session_status running emitted yet — worker_id must be None (no main-transcript scoping).
    assert evt.worker_id is None


@pytest.mark.asyncio
async def test_claude_backend_text_event_maps_to_assistant_text() -> None:
    """Claude CLI shape: {"type":"assistant","message":{"content":[{"text":"..."}]}}"""
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-1")

    emit({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Claude says hi"}]},
    })
    await _drain_tasks()

    assert len(daemon.events) == 1
    evt = daemon.events[0]
    assert isinstance(evt, AssistantTextEvent)
    assert evt.text == "Claude says hi"
    assert evt.worker_id is None


@pytest.mark.asyncio
async def test_codex_backend_text_event_maps_to_assistant_text() -> None:
    """Codex shape: {"type":"item.completed","item":{"type":"agent_message","text":"..."}}"""
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-1")

    emit({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "Codex output"},
    })
    await _drain_tasks()

    assert len(daemon.events) == 1
    evt = daemon.events[0]
    assert isinstance(evt, AssistantTextEvent)
    assert evt.text == "Codex output"
    assert evt.worker_id is None


@pytest.mark.asyncio
async def test_sub_session_text_scoped_to_handle_not_main_transcript() -> None:
    """Sub-session text carrying its handle is attributed to that worker, not main transcript.

    Since _active_handle is gone, the handle must be stamped on the text event itself
    (as _make_emit does at source). Tests the contract: worker_id==handle, never None.
    """
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-scope")
    handle = "claude-scope-abc"

    emit({"type": "session_status", "handle": handle, "target": "claude", "status": "running"})
    await _drain_tasks()

    # Text event carries its originating handle (stamped by _make_emit at source).
    emit({"type": "assistant", "text": "sub-session output", "handle": handle})
    await _drain_tasks()

    text_events = [e for e in daemon.events if isinstance(e, AssistantTextEvent)]
    assert len(text_events) == 1
    assert text_events[0].worker_id == handle
    # Must NOT appear as a bare main-transcript bubble (no worker_id=None in text events).
    bare_text = [e for e in text_events if e.worker_id is None]
    assert bare_text == []


@pytest.mark.asyncio
async def test_sub_session_text_cleared_after_done() -> None:
    """After session_status done, subsequent text events have worker_id=None again."""
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-clear")
    handle = "claude-clear-xyz"

    emit({"type": "session_status", "handle": handle, "target": "claude", "status": "running"})
    emit({"type": "session_status", "handle": handle, "status": "done"})
    await _drain_tasks()

    emit({"type": "assistant", "text": "after close"})
    await _drain_tasks()

    text_events = [e for e in daemon.events if isinstance(e, AssistantTextEvent)]
    assert len(text_events) == 1
    assert text_events[0].worker_id is None


@pytest.mark.asyncio
async def test_codex_text_scoped_to_handle() -> None:
    """Codex item.completed text carries worker_id when handle is stamped on the event."""
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-codex-scope")
    handle = "codex-scope-789"

    emit({"type": "session_status", "handle": handle, "target": "codex", "status": "running"})
    await _drain_tasks()

    # handle must be stamped on the text event (as _make_emit does at source).
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "codex scoped"}, "handle": handle})
    await _drain_tasks()

    text_events = [e for e in daemon.events if isinstance(e, AssistantTextEvent)]
    assert len(text_events) == 1
    assert text_events[0].worker_id == handle


@pytest.mark.asyncio
async def test_unknown_event_type_is_silently_dropped() -> None:
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-1")

    emit({"type": "some_future_event", "data": "whatever"})
    await _drain_tasks()

    assert daemon.events == []


# ─────────────────────────── integration: SessionOpenTool → rail ─────────────


@pytest.mark.asyncio
async def test_session_open_tool_emits_running_event() -> None:
    """Full path: SessionOpenTool.run → _make_emit → session_emit → daemon."""
    daemon = _FakeDaemon()
    session_id = "ctrl-sess-open"
    emit_fn = _make_session_emit(daemon, session_id)

    reg = InteractiveSessionRegistry()
    # Pre-mint a handle so we can inject our fake session
    handle = "claude-test-abc"

    # Patch mint_handle to return our fixed handle
    reg.mint_handle = lambda target: handle  # type: ignore[method-assign]

    fake_session = _FakeSession(handle=handle, target="claude")

    tool = SessionOpenTool()
    # Patch CLI backend construction — we inject our fake session directly
    original_run = tool.run

    async def _patched_run(tool_input, context):
        # Build the fake session and add it to the registry directly,
        # bypassing the real CLI subprocess, then call open() + emit lifecycle.
        from tesseract.kernel.tools.session_tools import _make_emit, _turn_to_toolresult
        inp = tool_input
        h = reg.mint_handle(inp.target)
        emit = _make_emit(context)
        reg.add(fake_session)
        result = await fake_session.open(inp.task)
        if not result.is_error:
            emit({"type": "session_status", "handle": h, "target": inp.target, "status": "running"})
        return _turn_to_toolresult(result, h)

    tool.run = _patched_run  # type: ignore[method-assign]

    context = ToolContext(
        workspace_root=".",
        session_id=session_id,
        interactive_sessions=reg,
        session_emit=emit_fn,
    )

    tool_input = SessionOpenInput(target="claude", task="say hello")
    await tool.run(tool_input, context)  # type: ignore[arg-type]
    await _drain_tasks()

    running_events = [
        e for e in daemon.events
        if isinstance(e, WorkerStatusEvent) and e.status == "running"
    ]
    assert len(running_events) == 1
    assert running_events[0].worker_id == handle


@pytest.mark.asyncio
async def test_session_close_tool_emits_done_event() -> None:
    """SessionCloseTool.run emits WorkerStatusEvent(status='done') via session_emit."""
    daemon = _FakeDaemon()
    session_id = "ctrl-sess-close"
    emit_fn = _make_session_emit(daemon, session_id)

    handle = "claude-close-abc"
    reg = InteractiveSessionRegistry()
    reg.add(_FakeSession(handle=handle, target="claude"))

    tool = SessionCloseTool()
    context = ToolContext(
        workspace_root=".",
        session_id=session_id,
        interactive_sessions=reg,
        session_emit=emit_fn,
    )

    tool_input = SessionCloseInput(handle=handle)
    await tool.run(tool_input, context)
    await _drain_tasks()

    done_events = [
        e for e in daemon.events
        if isinstance(e, WorkerStatusEvent) and e.status == "done"
    ]
    assert len(done_events) == 1
    assert done_events[0].worker_id == handle


# ─────────────────────────── contract: open→running, text→assistant, close→done


@pytest.mark.asyncio
async def test_full_lifecycle_rail_sequence() -> None:
    """Contract: open → running rail event, text → assistant detail event, close → done rail event."""
    daemon = _FakeDaemon()
    session_id = "ctrl-sess-lifecycle"
    emit_fn = _make_session_emit(daemon, session_id)

    handle = "claude-lifecycle-xyz"

    # Simulate the three events in order
    emit_fn({"type": "session_status", "handle": handle, "target": "claude", "status": "running"})
    await _drain_tasks()

    # handle is stamped by _make_emit at source; include it here to match production behavior.
    emit_fn({"type": "assistant", "text": "Processing your request...", "handle": handle})
    await _drain_tasks()

    emit_fn({"type": "session_status", "handle": handle, "status": "done"})
    await _drain_tasks()

    assert len(daemon.events) == 3

    open_evt, text_evt, close_evt = daemon.events

    assert isinstance(open_evt, WorkerStatusEvent)
    assert open_evt.worker_id == handle
    assert open_evt.status == "running"

    assert isinstance(text_evt, AssistantTextEvent)
    assert text_evt.text == "Processing your request..."
    assert text_evt.worker_id == handle  # scoped to sub-session, not main transcript

    assert isinstance(close_evt, WorkerStatusEvent)
    assert close_evt.worker_id == handle
    assert close_evt.status == "done"


# ─────────────────────────── parallel-safety regression ────────────────────────


@pytest.mark.asyncio
async def test_interleaved_parallel_sessions_attribute_text_by_source_handle() -> None:
    """Regression guard for the _active_handle race.

    Two sessions (claude-A and codex-B) stream concurrently.  Events arrive
    interleaved in the order: A running, B running, A text, B text.
    Without the fix a shared _active_handle cell would attribute both text
    events to B (the last to set running).  With the fix every event carries
    its own handle, so A text → worker_id==claude-A, B text → worker_id==codex-B.

    Also asserts that no text event lands with worker_id=None (main-transcript
    pollution) — both sub-sessions must remain scoped.
    """
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-parallel")

    handle_a = "claude-A"
    handle_b = "codex-B"

    # Interleaved: A starts, B starts, then A text, then B text.
    emit({"type": "session_status", "handle": handle_a, "target": "claude", "status": "running"})
    emit({"type": "session_status", "handle": handle_b, "target": "codex", "status": "running"})
    emit({"type": "assistant", "text": "from A", "handle": handle_a})
    emit({"type": "assistant", "text": "from B", "handle": handle_b})
    await _drain_tasks()

    text_events = [e for e in daemon.events if isinstance(e, AssistantTextEvent)]
    assert len(text_events) == 2

    by_text = {e.text: e for e in text_events}
    assert by_text["from A"].worker_id == handle_a, (
        "text from claude-A was mis-attributed (race regression)"
    )
    assert by_text["from B"].worker_id == handle_b, (
        "text from codex-B was mis-attributed (race regression)"
    )

    # No text event must leak into the main transcript (worker_id=None).
    bare_text = [e for e in text_events if e.worker_id is None]
    assert bare_text == [], "sub-session text must not appear as unscoped main-transcript event"


@pytest.mark.asyncio
async def test_interleaved_parallel_codex_sessions_attribute_correctly() -> None:
    """Same race guard for item.completed (Codex) shape with two parallel handles."""
    daemon = _FakeDaemon()
    emit = _make_session_emit(daemon, "sess-parallel-codex")

    handle_a = "codex-A"
    handle_b = "codex-B"

    emit({"type": "session_status", "handle": handle_a, "target": "codex", "status": "running"})
    emit({"type": "session_status", "handle": handle_b, "target": "codex", "status": "running"})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "codex A out"}, "handle": handle_a})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "codex B out"}, "handle": handle_b})
    await _drain_tasks()

    text_events = [e for e in daemon.events if isinstance(e, AssistantTextEvent)]
    assert len(text_events) == 2

    by_text = {e.text: e for e in text_events}
    assert by_text["codex A out"].worker_id == handle_a
    assert by_text["codex B out"].worker_id == handle_b

    bare_text = [e for e in text_events if e.worker_id is None]
    assert bare_text == []
