"""Integration test for the C1/C2 dispatch bridge (Audit-3).

Drives a fake ``ChatSession`` whose ``send()`` yields a representative
sequence of ``StreamChunk`` values through
``ControllerRuntime.make_dispatch_turn`` and asserts the daemon
receives typed transcript events in the right order — not one big
batched ``assistant_text`` row at the end.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.kernel.state import ToolCall
from tesseract.scripts.tars_controller import ControllerRuntime


class _FakeChatSession:
    def __init__(self, script: list[StreamChunk]) -> None:
        self._script = script
        self.history: list[dict[str, Any]] = []

    async def send(self, text: str):
        for chunk in self._script:
            yield chunk


class _FakeRecord:
    def __init__(self, sid: str) -> None:
        self.session_id = sid


class _FakeDaemon:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append_event(self, session_id: str, event: Any) -> None:
        self.events.append(event)


def _make_script_with_tool() -> list[StreamChunk]:
    return [
        StreamChunk(type=ChunkType.TEXT, text="Looking up the file. "),
        StreamChunk(type=ChunkType.TEXT, text="One sec."),
        StreamChunk(
            type=ChunkType.TOOL_CALL_END,
            tool_call=ToolCall(
                id="call-1", name="file_read", input={"file_path": "x.md"}
            ),
        ),
        StreamChunk(
            type=ChunkType.TOOL_RESULT,
            text="file contents here",
            tool_call_id="call-1",
        ),
        StreamChunk(type=ChunkType.TEXT, text="Here's the answer."),
        StreamChunk(
            type=ChunkType.STOP,
            stop_reason="end_turn",
            raw={"usage": {"input_tokens": 100, "output_tokens": 25}},
        ),
    ]


@pytest.mark.asyncio
async def test_dispatch_emits_typed_events_in_order() -> None:
    runtime = ControllerRuntime()
    # Brain wiring is normally built at boot. For the test we set the
    # holders directly so dispatch_turn doesn't bail at the
    # `adapter is None` guard, and stub _build_chat_session to return
    # our scripted fake.
    runtime.adapter = object()
    runtime.system_prompt = "test prompt"
    script = _make_script_with_tool()
    runtime._build_chat_session = lambda record, daemon: _FakeChatSession(script)  # type: ignore[method-assign]
    dispatch = runtime.make_dispatch_turn()

    daemon = _FakeDaemon()
    await dispatch(_FakeRecord("sess-test"), "hi", daemon)  # type: ignore[arg-type]

    kinds = [e.kind for e in daemon.events]
    # Expected sequence:
    #   metrics(thinking) → many assistant_text(partial=True) →
    #   assistant_text(partial=False, close) → tool_use →
    #   metrics(tool) → tool_result → metrics(streaming) →
    #   assistant_text(partial=True) → assistant_text(close) →
    #   metrics(done)
    assert kinds[0] == "session_metrics"
    assert "tool_use" in kinds
    assert "tool_result" in kinds
    assert kinds.count("assistant_text") >= 4
    assert kinds[-1] == "session_metrics"
    # The tool_use must come strictly before its tool_result.
    assert kinds.index("tool_use") < kinds.index("tool_result")


@pytest.mark.asyncio
async def test_tool_output_is_not_mixed_into_assistant_text() -> None:
    """C2 regression: previously every chunk with a .text attribute was
    concatenated into one assistant_text, so a TOOL_RESULT's body
    arrived as model prose."""
    runtime = ControllerRuntime()
    runtime.adapter = object()
    runtime.system_prompt = "test prompt"
    runtime._build_chat_session = lambda r, d: _FakeChatSession(  # type: ignore[method-assign]
        _make_script_with_tool()
    )
    dispatch = runtime.make_dispatch_turn()
    daemon = _FakeDaemon()
    await dispatch(_FakeRecord("sess-x"), "hi", daemon)  # type: ignore[arg-type]

    # Concatenate every assistant_text event's text — the tool result's
    # body must NOT appear in there.
    assistant_text = "".join(
        getattr(e, "text", "")
        for e in daemon.events
        if e.kind == "assistant_text"
    )
    assert "file contents here" not in assistant_text
    # But it must be present as a tool_result event's output.
    tool_results = [e for e in daemon.events if e.kind == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].output == "file contents here"
    assert tool_results[0].success is True


@pytest.mark.asyncio
async def test_dispatch_skipped_when_brain_not_ready() -> None:
    """The boot-time TC-4 invariant: dispatch is a no-op when the
    adapter or system prompt is unset (brain rebuild failed)."""
    runtime = ControllerRuntime()
    # adapter / system_prompt remain None — initial_build was never called.
    dispatch = runtime.make_dispatch_turn()
    daemon = _FakeDaemon()
    await dispatch(_FakeRecord("sess-y"), "hi", daemon)  # type: ignore[arg-type]
    assert daemon.events == []


@pytest.mark.asyncio
async def test_dispatch_recovers_from_exception() -> None:
    """An exception in the stream surfaces a user-visible error event
    instead of bubbling up and crashing the daemon-side dispatcher."""

    class _BoomSession:
        history: list[dict[str, Any]] = []

        async def send(self, _text: str):
            yield StreamChunk(type=ChunkType.TEXT, text="started ")
            raise RuntimeError("boom")

    runtime = ControllerRuntime()
    runtime.adapter = object()
    runtime.system_prompt = "test"
    runtime._build_chat_session = lambda r, d: _BoomSession()  # type: ignore[method-assign]
    dispatch = runtime.make_dispatch_turn()
    daemon = _FakeDaemon()
    await dispatch(_FakeRecord("sess-z"), "hi", daemon)  # type: ignore[arg-type]

    # Should have seen the partial text, then an error bubble, then a
    # metrics(error) update.
    kinds = [e.kind for e in daemon.events]
    assert "assistant_text" in kinds
    assert kinds[-1] == "session_metrics"
    # Last metrics event marks the turn as errored.
    metrics = [e for e in daemon.events if e.kind == "session_metrics"]
    assert metrics[-1].turn_state == "error"


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(test_dispatch_emits_typed_events_in_order())
