"""CR-4 — fake adapter stream with ``tool_start`` / ``tool_end`` envelopes
produces the matching ``on_progress`` calls.

Verifies the wiring inside ``_start_channel_turn``: TOOL_CALL_START maps
to ``ProgressEvent(kind="tool_start", tool_name=…)`` with the tool's
input args carried through; TOOL_RESULT maps to ``kind="tool_end"`` and
re-uses the cached start (so the result chunk doesn't need the name).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import pytest

from tesseract.integrations import _channel_progress as cp
from tesseract.kernel.adapters.base import ChunkType, StreamChunk
from tesseract.kernel.state import ToolCall
from tesseract.mirror.server import ws as ws_module


@dataclass
class _FakeToolContext:
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _FakeChatSession:
    chunks: list[StreamChunk]
    tool_context: _FakeToolContext = field(default_factory=_FakeToolContext)
    history: list[dict[str, Any]] = field(default_factory=list)

    async def send(self, body: str) -> AsyncIterator[StreamChunk]:
        del body
        for ch in self.chunks:
            yield ch
        self.history.append({"role": "assistant", "content": "ok"})


@dataclass
class _FakeSession:
    session_id: str
    chat_session: _FakeChatSession
    current_turn_task: asyncio.Task[None] | None = None


@pytest.mark.asyncio
async def test_tool_start_and_result_surface_as_progress_events(monkeypatch):
    # Suppress the elapsed pump so we only assert on tool-lifecycle events.
    monkeypatch.setattr(cp, "ELAPSED_TICKS_S", (3600.0,))
    events: list[cp.ProgressEvent] = []

    async def _on_progress(event):
        events.append(event)

    tc = ToolCall(id="call_1", name="web_search", input={"query": "el niño 2026"})
    chunks = [
        StreamChunk(type=ChunkType.TOOL_CALL_START, tool_call_id="call_1", tool_call=tc),
        StreamChunk(type=ChunkType.TOOL_CALL_END, tool_call_id="call_1", tool_call=tc),
        StreamChunk(type=ChunkType.TOOL_RESULT, tool_call_id="call_1", text="…"),
        StreamChunk(type=ChunkType.TEXT, text="<answer>here you go</answer>"),
    ]
    session = _FakeSession(
        session_id="s_tool",
        chat_session=_FakeChatSession(chunks=chunks),
    )
    reply = await ws_module._start_channel_turn(
        app=None,
        session=session,
        channel="telegram",
        chat_id="99",
        body="search please",
        on_progress=_on_progress,
    )
    assert reply == "here you go"

    kinds = [(e.kind, e.tool_name) for e in events]
    assert ("tool_start", "web_search") in kinds
    assert ("tool_end", "web_search") in kinds

    # Args propagate so format_progress_line can render the query.
    start_evt = next(e for e in events if e.kind == "tool_start")
    assert start_evt.tool_args == {"query": "el niño 2026"}


@pytest.mark.asyncio
async def test_progress_callback_exception_is_swallowed(monkeypatch):
    """A broken on_progress callback must not abort the turn — the
    operator-visible reply still lands."""
    monkeypatch.setattr(cp, "ELAPSED_TICKS_S", (3600.0,))

    async def _boom(event):
        raise RuntimeError("nope")

    tc = ToolCall(id="c", name="memory_search", input={})
    chunks = [
        StreamChunk(type=ChunkType.TOOL_CALL_START, tool_call_id="c", tool_call=tc),
        StreamChunk(type=ChunkType.TEXT, text="<answer>done</answer>"),
    ]
    session = _FakeSession(
        session_id="s_boom",
        chat_session=_FakeChatSession(chunks=chunks),
    )
    reply = await ws_module._start_channel_turn(
        app=None,
        session=session,
        channel="telegram",
        chat_id="99",
        body="ping",
        on_progress=_boom,
    )
    assert reply == "done"


@pytest.mark.asyncio
async def test_no_progress_callback_disables_lifecycle_tracking(monkeypatch):
    """When ``on_progress=None`` the elapsed pump must not even start
    and nothing observable changes — backwards-compatible default."""
    monkeypatch.setattr(cp, "ELAPSED_TICKS_S", (0.05,))

    tc = ToolCall(id="c2", name="web_search", input={"query": "x"})
    chunks = [
        StreamChunk(type=ChunkType.TOOL_CALL_START, tool_call_id="c2", tool_call=tc),
        StreamChunk(type=ChunkType.TOOL_RESULT, tool_call_id="c2", text="…"),
        StreamChunk(type=ChunkType.TEXT, text="<answer>ok</answer>"),
    ]
    session = _FakeSession(
        session_id="s_noprog",
        chat_session=_FakeChatSession(chunks=chunks),
    )
    reply = await ws_module._start_channel_turn(
        app=None,
        session=session,
        channel="telegram",
        chat_id="99",
        body="hi",
    )
    assert reply == "ok"
