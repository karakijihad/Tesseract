"""2026-07-16 adapter-parity fix — openai-compat streaming.

Live-probed against xAI grok-4.3 (2026-07-16): with
`stream_options.include_usage` the usage object arrives in a dedicated
final chunk whose `choices` array is EMPTY, after the finish_reason chunk.
The old `_do_stream` skipped empty-choice chunks before reading usage and
emitted STOP at finish_reason — so the cost ledger recorded $0 for every
spec-faithful provider (the "grok $0 spend" bug). Reasoning models also
stream chain-of-thought in `delta.reasoning_content`, which was dropped.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from tesseract.kernel.adapters.base import ChunkType
from tesseract.kernel.adapters.openai import OpenAIAdapter


def _adapter(**kw: Any) -> OpenAIAdapter:
    return OpenAIAdapter(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        timeout=1.0,
        max_retries=1,
        **kw,
    )


def _delta(content: str | None = None, tool_calls: Any = None, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=tool_calls, **extra)


def _chunk(
    delta: SimpleNamespace | None = None,
    finish_reason: str | None = None,
    usage: Any = None,
) -> SimpleNamespace:
    choices = []
    if delta is not None or finish_reason is not None:
        choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    return SimpleNamespace(choices=choices, usage=usage)


def _usage(prompt: int, completion: int, cached: int | None = None) -> SimpleNamespace:
    details = SimpleNamespace(cached_tokens=cached) if cached is not None else None
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=details,
    )


class _FakeStream:
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


def _stub_client(adapter: OpenAIAdapter, chunks: list[SimpleNamespace]) -> dict[str, Any]:
    """Replace the SDK client with a stub; returns the captured kwargs dict."""
    captured: dict[str, Any] = {}

    async def create(**kwargs: Any):
        captured.update(kwargs)
        return _FakeStream(chunks)

    adapter.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return captured


def _run(adapter: OpenAIAdapter, messages: list[dict[str, Any]]) -> list[Any]:
    async def collect() -> list[Any]:
        return [c async for c in adapter.stream(messages)]

    return asyncio.run(collect())


def test_usage_in_trailing_empty_choices_chunk_reaches_stop() -> None:
    """The grok/OpenAI spec shape: finish_reason chunk first, then a usage
    chunk with empty `choices`. STOP must carry that usage."""
    adapter = _adapter()
    _stub_client(adapter, [
        _chunk(_delta(content="hi")),
        _chunk(_delta(), finish_reason="stop"),
        _chunk(usage=_usage(279, 11, cached=192)),  # choices == []
    ])
    chunks = _run(adapter, [{"role": "user", "content": "x"}])
    stop = chunks[-1]
    assert stop.type == ChunkType.STOP
    assert stop.raw["usage"] == {
        "input_tokens": 279,
        "output_tokens": 11,
        "cached_tokens": 192,
    }


def test_usage_on_finish_chunk_still_works() -> None:
    """The NIM shape: usage attached to the finish_reason chunk itself."""
    adapter = _adapter()
    _stub_client(adapter, [
        _chunk(_delta(content="hi")),
        _chunk(_delta(), finish_reason="stop", usage=_usage(10, 5)),
    ])
    chunks = _run(adapter, [{"role": "user", "content": "x"}])
    stop = chunks[-1]
    assert stop.type == ChunkType.STOP
    assert stop.raw["usage"]["input_tokens"] == 10
    assert stop.raw["usage"]["output_tokens"] == 5


def test_single_stop_emitted_and_stop_reason_survives_late_usage() -> None:
    adapter = _adapter()
    tc = SimpleNamespace(
        index=0,
        id="call-1",
        function=SimpleNamespace(name="get_time", arguments='{"city": "Tokyo"}'),
    )
    _stub_client(adapter, [
        _chunk(_delta(tool_calls=[tc])),
        _chunk(_delta(), finish_reason="tool_calls"),
        _chunk(usage=_usage(1, 2)),
    ])
    chunks = _run(adapter, [{"role": "user", "content": "x"}])
    stops = [c for c in chunks if c.type == ChunkType.STOP]
    assert len(stops) == 1
    assert stops[0].stop_reason == "tool_use"
    ends = [c for c in chunks if c.type == ChunkType.TOOL_CALL_END]
    assert len(ends) == 1
    assert ends[0].tool_call.input == {"city": "Tokyo"}


def test_cache_routing_header_sent_and_stable_across_dynamic_tail() -> None:
    """xAI cache routing (`x-grok-conv-id`): the header value must derive
    from the system-prompt PREFIX only, so the ephemeral "Right now" tail
    (minute-level clock) doesn't re-route every turn to a new cache node."""
    static_prefix = "IDENTITY " * 400  # > _ROUTING_KEY_PREFIX_CHARS chars
    captured_values = []
    for tail in ("Local time: 20:21", "Local time: 20:22"):
        adapter = _adapter(cache_routing_header="x-grok-conv-id")
        captured = _stub_client(adapter, [_chunk(_delta(content="hi"), finish_reason="stop")])
        _run(adapter, [
            {"role": "system", "content": static_prefix + tail},
            {"role": "user", "content": "x"},
        ])
        headers = captured.get("extra_headers")
        assert headers is not None and "x-grok-conv-id" in headers
        captured_values.append(headers["x-grok-conv-id"])
    assert captured_values[0] == captured_values[1]


def test_no_routing_header_by_default() -> None:
    """Providers without `cache_routing_header` must not grow extra headers."""
    adapter = _adapter()
    captured = _stub_client(adapter, [_chunk(_delta(content="hi"), finish_reason="stop")])
    _run(adapter, [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x"},
    ])
    assert "extra_headers" not in captured


def test_reasoning_content_streams_as_thinking() -> None:
    """xAI/DeepSeek/GLM chain-of-thought must surface as THINKING chunks,
    never as TEXT and never silently dropped."""
    adapter = _adapter()
    _stub_client(adapter, [
        _chunk(_delta(reasoning_content="pondering...")),
        _chunk(_delta(content="answer")),
        _chunk(_delta(), finish_reason="stop"),
    ])
    chunks = _run(adapter, [{"role": "user", "content": "x"}])
    thinking = [c for c in chunks if c.type == ChunkType.THINKING]
    texts = [c for c in chunks if c.type == ChunkType.TEXT]
    assert [t.thinking for t in thinking] == ["pondering..."]
    assert [t.text for t in texts] == ["answer"]


def test_stream_options_sent_by_default_and_suppressible() -> None:
    adapter = _adapter()
    captured = _stub_client(adapter, [_chunk(_delta(), finish_reason="stop")])
    _run(adapter, [{"role": "user", "content": "x"}])
    assert captured["stream_options"] == {"include_usage": True}

    adapter_off = _adapter(supports_stream_usage=False)
    captured_off = _stub_client(adapter_off, [_chunk(_delta(), finish_reason="stop")])
    _run(adapter_off, [{"role": "user", "content": "x"}])
    assert "stream_options" not in captured_off


def test_stream_without_finish_reason_still_stops() -> None:
    """A stream that dies without finish_reason must still emit STOP so
    callers terminate instead of hanging on a missing sentinel."""
    adapter = _adapter()
    _stub_client(adapter, [_chunk(_delta(content="partial"))])
    chunks = _run(adapter, [{"role": "user", "content": "x"}])
    assert chunks[-1].type == ChunkType.STOP
    assert chunks[-1].stop_reason == "end_turn"
