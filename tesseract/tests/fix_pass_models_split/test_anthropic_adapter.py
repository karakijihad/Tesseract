"""AnthropicAdapter behavior tests.

We don't hit the real API. The SDK's `client.messages.stream()` is patched to
return an async context manager whose iterator yields canned event objects;
the adapter must translate them into the shared StreamChunk format used by
the rest of the kernel.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.kernel.adapters.anthropic import AnthropicAdapter, _to_anthropic_messages
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType


@dataclass
class _Event:
    type: str
    delta: Any = None
    content_block: Any = None
    index: int = -1
    message: Any = None
    usage: Any = None


class _FakeStream:
    """Async context manager mimicking client.messages.stream(...)."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.entered_with: dict[str, Any] | None = None

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN001
        return None

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()


def _adapter_with_events(events: list[Any]) -> tuple[AnthropicAdapter, MagicMock]:
    adapter = AnthropicAdapter.__new__(AnthropicAdapter)  # bypass __init__ (no SDK import)
    adapter.max_retries = 1
    fake_client = MagicMock()
    stream_mock = MagicMock(return_value=_FakeStream(events))
    fake_client.messages.stream = stream_mock
    adapter.client = fake_client
    return adapter, stream_mock


# ── Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_stream_emits_text_chunks() -> None:
    events = [
        _Event("message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=42))),
        _Event("content_block_start", index=0,
               content_block=SimpleNamespace(type="text")),
        _Event("content_block_delta", index=0,
               delta=SimpleNamespace(type="text_delta", text="Hello, ")),
        _Event("content_block_delta", index=0,
               delta=SimpleNamespace(type="text_delta", text="world!")),
        _Event("content_block_stop", index=0),
        _Event("message_delta",
               delta=SimpleNamespace(stop_reason="end_turn"),
               usage=SimpleNamespace(output_tokens=8)),
    ]
    adapter, _ = _adapter_with_events(events)
    chunks = []
    async for c in adapter.stream(
        [{"role": "user", "content": "hi"}],
        options=AdapterOptions(model="claude-haiku-4-5", max_output_tokens=512, temperature=0.7),
    ):
        chunks.append(c)

    text_chunks = [c for c in chunks if c.type == ChunkType.TEXT]
    assert "".join(c.text for c in text_chunks) == "Hello, world!"

    stop = chunks[-1]
    assert stop.type == ChunkType.STOP
    assert stop.stop_reason == "end_turn"
    assert stop.raw["usage"]["input_tokens"] == 42
    assert stop.raw["usage"]["output_tokens"] == 8


@pytest.mark.asyncio
async def test_tool_use_emits_start_delta_end() -> None:
    events = [
        _Event("message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=10))),
        _Event("content_block_start", index=0,
               content_block=SimpleNamespace(type="tool_use", id="toolu_01", name="memory_search")),
        _Event("content_block_delta", index=0,
               delta=SimpleNamespace(type="input_json_delta", partial_json='{"query":')),
        _Event("content_block_delta", index=0,
               delta=SimpleNamespace(type="input_json_delta", partial_json=' "alpha"}')),
        _Event("content_block_stop", index=0),
        _Event("message_delta",
               delta=SimpleNamespace(stop_reason="tool_use"),
               usage=SimpleNamespace(output_tokens=5)),
    ]
    adapter, _ = _adapter_with_events(events)
    chunks = []
    async for c in adapter.stream([{"role": "user", "content": "search"}],
                                  options=AdapterOptions(model="claude-haiku-4-5",
                                                         max_output_tokens=128, temperature=0.7)):
        chunks.append(c)

    starts = [c for c in chunks if c.type == ChunkType.TOOL_CALL_START]
    deltas = [c for c in chunks if c.type == ChunkType.TOOL_CALL_DELTA]
    ends = [c for c in chunks if c.type == ChunkType.TOOL_CALL_END]

    assert len(starts) == 1
    assert starts[0].tool_call.name == "memory_search"
    assert starts[0].tool_call_id == "toolu_01"

    assert len(deltas) == 2
    assert "".join(c.text for c in deltas) == '{"query": "alpha"}'

    assert len(ends) == 1
    assert ends[0].tool_call.input == {"query": "alpha"}

    assert chunks[-1].type == ChunkType.STOP
    assert chunks[-1].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_system_prompt_split_into_cache_block() -> None:
    """The system message becomes a cache_control'd top-level system block."""
    adapter, stream_mock = _adapter_with_events([
        _Event("message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=1))),
        _Event("message_delta", delta=SimpleNamespace(stop_reason="end_turn"),
               usage=SimpleNamespace(output_tokens=0)),
    ])
    messages = [
        {"role": "system", "content": "You are TARS."},
        {"role": "user", "content": "Hi."},
    ]
    async for _ in adapter.stream(messages, options=AdapterOptions(
            model="claude-haiku-4-5", max_output_tokens=64, temperature=0.7)):
        pass
    kwargs = stream_mock.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["max_tokens"] == 64
    # System extracted out of messages list
    assert kwargs["messages"] == [{"role": "user", "content": "Hi."}]
    # System carries cache_control
    assert kwargs["system"] == [{
        "type": "text",
        "text": "You are TARS.",
        "cache_control": {"type": "ephemeral"},
    }]


@pytest.mark.asyncio
async def test_tools_translated_to_anthropic_shape() -> None:
    adapter, stream_mock = _adapter_with_events([
        _Event("message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=1))),
        _Event("message_delta", delta=SimpleNamespace(stop_reason="end_turn"),
               usage=SimpleNamespace(output_tokens=0)),
    ])
    tools = [
        {"name": "memory_search", "description": "find stuff",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    ]
    async for _ in adapter.stream(
        [{"role": "user", "content": "go"}],
        tools=tools,
        options=AdapterOptions(model="claude-haiku-4-5", max_output_tokens=64, temperature=0.7),
    ):
        pass
    kwargs = stream_mock.call_args.kwargs
    assert kwargs["tools"] == [{
        "name": "memory_search",
        "description": "find stuff",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]


@pytest.mark.asyncio
async def test_cache_read_tokens_surface_in_usage() -> None:
    events = [
        _Event("message_start", message=SimpleNamespace(usage=SimpleNamespace(
            input_tokens=100, cache_read_input_tokens=80, cache_creation_input_tokens=0))),
        _Event("message_delta",
               delta=SimpleNamespace(stop_reason="end_turn"),
               usage=SimpleNamespace(output_tokens=12)),
    ]
    adapter, _ = _adapter_with_events(events)
    chunks = []
    async for c in adapter.stream([{"role": "user", "content": "x"}],
                                  options=AdapterOptions(model="claude-haiku-4-5",
                                                         max_output_tokens=32, temperature=0.7)):
        chunks.append(c)
    stop = chunks[-1]
    assert stop.raw["usage"]["cached_tokens"] == 80
    assert stop.raw["usage"]["cache_creation_tokens"] == 0


def test_split_system_concatenates_multiple() -> None:
    msgs = [
        {"role": "system", "content": "One."},
        {"role": "system", "content": "Two."},
        {"role": "user", "content": "Hi"},
    ]
    sys, rest = _to_anthropic_messages(msgs)
    assert sys == "One.\n\nTwo."
    assert rest == [{"role": "user", "content": "Hi"}]


def test_tool_round_trip_converts_to_anthropic_shape() -> None:
    """OpenAI-shape assistant tool_calls + role:"tool" results → tool_use /
    tool_result blocks. Consecutive tool results fold into ONE user message."""
    msgs = [
        {"role": "user", "content": "check both files"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "file_read", "arguments": '{"file_path": "a.py"}'}},
                {"id": "c2", "type": "function",
                 "function": {"name": "file_read", "arguments": '{"file_path": "b.py"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "contents A"},
        {"role": "tool", "tool_call_id": "c2", "content": "contents B"},
    ]
    _sys, rest = _to_anthropic_messages(msgs)
    assert rest[1]["role"] == "assistant"
    uses = [b for b in rest[1]["content"] if b["type"] == "tool_use"]
    assert [u["id"] for u in uses] == ["c1", "c2"]
    assert uses[0]["input"] == {"file_path": "a.py"}
    # both results in a single following user message
    assert len(rest) == 3
    results = rest[2]["content"]
    assert rest[2]["role"] == "user"
    assert [b["tool_use_id"] for b in results] == ["c1", "c2"]
    assert all(b["type"] == "tool_result" for b in results)


def test_orphan_tool_result_is_stripped() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "gone", "content": "orphaned"},
    ]
    _sys, rest = _to_anthropic_messages(msgs)
    assert rest == [{"role": "user", "content": "hi"}]


def test_count_tokens_returns_positive_estimate() -> None:
    adapter = AnthropicAdapter.__new__(AnthropicAdapter)
    n = adapter.count_tokens([{"role": "user", "content": "hello world"}])
    assert n >= 1
