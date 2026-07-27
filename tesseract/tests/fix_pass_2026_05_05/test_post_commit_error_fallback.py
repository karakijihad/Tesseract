"""Layer 1 (2026-05-05) — `FallbackAdapter` must surface a TRANSIENT-tagged
ERROR + record a breaker failure when an adapter emits a `ChunkType.ERROR`
*after* it has already committed (yielded TEXT / TOOL_CALL_*).

Before this fix, the post-commit ERROR fell through `elif committed: yield
chunk` and was forwarded to the caller raw — no breaker bump, no
classification. ChatSession.send() then `return`-ed silently and TARS
never saw the error. Mirrors the long-standing post-commit raised-
exception path (lines 334-347).
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from tesseract.brain.adapter_chain import FallbackAdapter
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ErrorKind,
    StreamChunk,
)


class _Adapter:
    def __init__(self, name: str, scenes: list[list[StreamChunk]]) -> None:
        self.model = name
        self._scenes = list(scenes)
        self.calls = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls += 1
        if not self._scenes:
            raise AssertionError(f"{self.model}: no scenes left")
        for chunk in self._scenes.pop(0):
            yield chunk

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _opts(model: str) -> AdapterOptions:
    return AdapterOptions(role="chat_brain", provider="test", model=model, tier="api")


@pytest.mark.asyncio
async def test_post_commit_error_yields_tagged_transient_and_returns() -> None:
    """Adapter streams TEXT then ERROR — caller receives the original TEXT
    and a follow-up ERROR with `error_kind=TRANSIENT`. No new fallback
    entry is invoked (mid-stream rewind would be unsafe)."""
    flaky = _Adapter("flaky", scenes=[[
        StreamChunk(type=ChunkType.TEXT, text="partial-"),
        StreamChunk(type=ChunkType.ERROR, error="OpenAI 500", error_kind=ErrorKind.UNKNOWN),
    ]])
    backup = _Adapter("backup", scenes=[[
        StreamChunk(type=ChunkType.TEXT, text="should-not-run"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ]])
    fb = FallbackAdapter(
        [(flaky, _opts("flaky")), (backup, _opts("backup"))],
        transient_retries=2,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    # Primary committed (TEXT yielded), then errored — backup is NOT tried.
    assert flaky.calls == 1
    assert backup.calls == 0

    text_chunks = [c for c in chunks if c.type == ChunkType.TEXT]
    assert any(c.text == "partial-" for c in text_chunks)

    err_chunks = [c for c in chunks if c.type == ChunkType.ERROR]
    assert len(err_chunks) == 1, "exactly one ERROR chunk should reach the caller"
    err = err_chunks[0]
    assert "after commit" in (err.error or "")
    assert "OpenAI 500" in (err.error or "")
    # Original kind is preserved for diagnostic fidelity. Retry decision
    # in ChatSession is driven by `raw['severity']`, not error_kind, so
    # the kind here is informational — UNKNOWN stays UNKNOWN.
    assert err.error_kind == ErrorKind.UNKNOWN


@pytest.mark.asyncio
async def test_post_commit_error_records_breaker_failure() -> None:
    """A post-commit ERROR must bump the breaker for that entry just
    like a post-commit raised exception does. Otherwise a chronically
    flaky provider that only fails after streaming a token never trips
    cooldown."""
    fake_now = [0.0]

    flaky = _Adapter("flaky", scenes=[[
        StreamChunk(type=ChunkType.TEXT, text="hi"),
        StreamChunk(type=ChunkType.ERROR, error="late 500", error_kind=ErrorKind.TRANSIENT),
    ]])
    backup = _Adapter("backup", scenes=[[
        StreamChunk(type=ChunkType.TEXT, text="recovered"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ]])
    fb = FallbackAdapter(
        [(flaky, _opts("flaky")), (backup, _opts("backup"))],
        transient_retries=0,
        transient_backoff_ms=0,
        cooldown_max_failures=1,
        cooldown_seconds=10.0,
        time_func=lambda: fake_now[0],
    )

    # First call: flaky errors post-commit. Breaker should flip open.
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]
    assert any(c.type == ChunkType.ERROR for c in chunks)
    assert fb._breakers[0].is_open(fake_now[0]), "post-commit ERROR must trip breaker"

    # Second call (within cooldown): flaky must be skipped, backup runs.
    fake_now[0] = 1.0
    flaky._scenes.append([
        StreamChunk(type=ChunkType.TEXT, text="should-be-skipped"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ])
    chunks2 = [c async for c in fb.stream(messages=[], tools=None, options=None)]
    assert flaky.calls == 1, "breaker open — flaky must be skipped"
    assert backup.calls == 1
    assert any(c.type == ChunkType.TEXT and c.text == "recovered" for c in chunks2)


@pytest.mark.asyncio
async def test_post_commit_error_carries_soft_envelope_metadata() -> None:
    """The yielded ERROR chunk must include `raw['severity']='soft'` plus
    structured fields (model, chain_index, provider_error, request_id) so
    the Mirror frontend can render an inline note instead of a turn-killing
    red card. `request_id` is parsed from the upstream provider message
    (OpenAI Responses surfaces `req_<hex>`)."""
    upstream = (
        "OpenAI Responses error: An error occurred while processing your "
        "request. Please include the request ID req_b41da9e09eae4a0d934d905070c772a5."
    )
    flaky = _Adapter("gpt-5.4-mini", scenes=[[
        StreamChunk(type=ChunkType.TEXT, text="partial-"),
        StreamChunk(type=ChunkType.ERROR, error=upstream, error_kind=ErrorKind.TRANSIENT),
    ]])
    fb = FallbackAdapter(
        [(flaky, _opts("gpt-5.4-mini"))],
        transient_retries=0,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    err = next(c for c in chunks if c.type == ChunkType.ERROR)
    raw = err.raw or {}
    assert raw.get("severity") == "soft"
    assert raw.get("kind") == "post_commit_partial"
    # `entry_label` resolves from the adapter's `model` attribute (per
    # adapter_chain.py:235) — the in-flight model identity, not the chain
    # entry's configured model name.
    assert raw.get("model") == "gpt-5.4-mini"
    assert raw.get("chain_index") == 0
    assert "An error occurred" in (raw.get("provider_error") or "")
    assert raw.get("request_id") == "req_b41da9e09eae4a0d934d905070c772a5"


@pytest.mark.asyncio
async def test_post_commit_exception_carries_soft_envelope_metadata() -> None:
    """Symmetric coverage for the raised-exception branch — adapter throws
    mid-stream after committing. The ERROR chunk must carry the same soft
    payload shape (kind='post_commit_exception')."""

    class _RaisingAdapter:
        model = "raise-mid"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, messages, tools=None, options=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            yield StreamChunk(type=ChunkType.TEXT, text="started ")
            raise RuntimeError("upstream eof req_abcdef0123456789abcdef0123")

        def count_tokens(self, messages):  # type: ignore[no-untyped-def]
            return 0

        async def check_available(self) -> bool:
            return True

    adapter = _RaisingAdapter()
    fb = FallbackAdapter(
        [(adapter, _opts("raise-mid"))],
        transient_retries=0,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    err = next(c for c in chunks if c.type == ChunkType.ERROR)
    raw = err.raw or {}
    assert raw.get("severity") == "soft"
    assert raw.get("kind") == "post_commit_exception"
    assert raw.get("model") == "raise-mid"
    assert raw.get("chain_index") == 0
    assert "upstream eof" in (raw.get("provider_error") or "")
    assert raw.get("request_id") == "req_abcdef0123456789abcdef0123"


@pytest.mark.asyncio
async def test_pre_commit_error_path_unchanged() -> None:
    """Regression: a *pre*-commit ERROR (no TEXT/TOOL chunks emitted yet)
    must still trigger normal chain advance, not the new post-commit
    branch. UNKNOWN classification → treated as TRANSIENT → retry then
    advance."""
    primary = _Adapter("primary", scenes=[
        [StreamChunk(type=ChunkType.ERROR, error="500", error_kind=ErrorKind.UNKNOWN)],
        [StreamChunk(type=ChunkType.ERROR, error="500", error_kind=ErrorKind.UNKNOWN)],
    ])
    secondary = _Adapter("secondary", scenes=[[
        StreamChunk(type=ChunkType.TEXT, text="from secondary"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ]])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=1,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 2  # 1 initial + 1 retry, then advance
    assert secondary.calls == 1
    assert any(c.type == ChunkType.TEXT and c.text == "from secondary" for c in chunks)
