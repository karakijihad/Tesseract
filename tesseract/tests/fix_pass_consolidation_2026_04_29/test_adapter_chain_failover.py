"""Audit M3 regression — `FallbackAdapter` must transparently fail over
to the next chain entry when the primary fails before committing to a
turn.

Before 2026-04-29 the chain was resolved at startup and stashed in
`app["adapter_chain"]` but never consumed by the chat loop. Mid-turn
provider failure surfaced as an ERROR chunk and ended the turn; the
operator had to manually flip models or restart the session.

Failover policy:
  - Pre-commit failure (raised exception or ERROR chunk before any
    TEXT/TOOL_CALL chunk) → swallow, advance to next entry.
  - Post-commit failure (TEXT or TOOL_CALL_END already streamed) → no
    rollback possible, surface the error and end the turn.
  - All entries failed pre-commit → final ERROR with the last reason.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

import pytest

from tesseract.brain.adapter_chain import FallbackAdapter
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


class _ScriptedAdapter(ModelAdapter):
    """Adapter that yields a scripted sequence of chunks then optionally raises."""

    def __init__(
        self,
        name: str,
        chunks: list[StreamChunk] | None = None,
        raise_at: int | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.name = name
        self._chunks = chunks or []
        self._raise_at = raise_at
        self._raise_exc = raise_exc or RuntimeError(f"{name}: scripted failure")
        self.calls = 0

    @property
    def model(self) -> str:
        return self.name

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls += 1
        for i, chunk in enumerate(self._chunks):
            if self._raise_at == i:
                raise self._raise_exc
            yield chunk
        if self._raise_at is not None and self._raise_at >= len(self._chunks):
            raise self._raise_exc

    def count_tokens(self, messages):
        return 0

    async def check_available(self):
        return True


def _opts(model: str) -> AdapterOptions:
    return AdapterOptions(model=model, provider="test")


def _collect(stream) -> list[StreamChunk]:
    async def _drain():
        return [c async for c in stream]

    return asyncio.run(_drain())


def test_primary_succeeds_no_fallover() -> None:
    primary = _ScriptedAdapter("primary", chunks=[
        StreamChunk(type=ChunkType.TEXT, text="hello"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ])
    secondary = _ScriptedAdapter("secondary", chunks=[
        StreamChunk(type=ChunkType.TEXT, text="should not run"),
    ])
    fb = FallbackAdapter([(primary, _opts("primary")), (secondary, _opts("secondary"))], transient_retries=0, transient_backoff_ms=0)
    chunks = _collect(fb.stream(messages=[]))
    assert primary.calls == 1
    assert secondary.calls == 0
    assert [c.type for c in chunks] == [ChunkType.TEXT, ChunkType.STOP]


def test_primary_raises_pre_commit_falls_over() -> None:
    primary = _ScriptedAdapter(
        "primary", chunks=[], raise_at=0, raise_exc=ConnectionError("upstream 503"),
    )
    secondary = _ScriptedAdapter("secondary", chunks=[
        StreamChunk(type=ChunkType.TEXT, text="from-secondary"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ])
    fb = FallbackAdapter([(primary, _opts("primary")), (secondary, _opts("secondary"))], transient_retries=0, transient_backoff_ms=0)
    chunks = _collect(fb.stream(messages=[]))
    assert primary.calls == 1
    assert secondary.calls == 1
    text = "".join(c.text for c in chunks if c.type == ChunkType.TEXT)
    assert text == "from-secondary"


def test_primary_yields_error_pre_commit_falls_over() -> None:
    primary = _ScriptedAdapter("primary", chunks=[
        StreamChunk(type=ChunkType.ERROR, error="rate limited"),
    ])
    secondary = _ScriptedAdapter("secondary", chunks=[
        StreamChunk(type=ChunkType.TEXT, text="recovered"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ])
    fb = FallbackAdapter([(primary, _opts("primary")), (secondary, _opts("secondary"))], transient_retries=0, transient_backoff_ms=0)
    chunks = _collect(fb.stream(messages=[]))
    types = [c.type for c in chunks]
    # No ERROR surfaced — fallover absorbed it.
    assert ChunkType.ERROR not in types
    assert any(c.type == ChunkType.TEXT and c.text == "recovered" for c in chunks)


def test_post_commit_failure_surfaces_error_no_fallover() -> None:
    """Once primary has streamed text, we can't safely roll back. The
    error must surface to the caller; secondary is NOT retried."""
    primary = _ScriptedAdapter(
        "primary",
        chunks=[StreamChunk(type=ChunkType.TEXT, text="partial...")],
        raise_at=1,
        raise_exc=RuntimeError("midstream"),
    )
    secondary = _ScriptedAdapter("secondary", chunks=[
        StreamChunk(type=ChunkType.TEXT, text="should not run"),
    ])
    fb = FallbackAdapter([(primary, _opts("primary")), (secondary, _opts("secondary"))], transient_retries=0, transient_backoff_ms=0)
    chunks = _collect(fb.stream(messages=[]))
    assert primary.calls == 1
    assert secondary.calls == 0  # post-commit, no fallover
    assert any(c.type == ChunkType.TEXT and c.text == "partial..." for c in chunks)
    error_chunks = [c for c in chunks if c.type == ChunkType.ERROR]
    assert len(error_chunks) == 1
    assert "after commit" in error_chunks[0].error


def test_all_fail_yields_final_error() -> None:
    a = _ScriptedAdapter("a", chunks=[StreamChunk(type=ChunkType.ERROR, error="a-fail")])
    b = _ScriptedAdapter("b", chunks=[StreamChunk(type=ChunkType.ERROR, error="b-fail")])
    fb = FallbackAdapter([(a, _opts("a")), (b, _opts("b"))], transient_retries=0, transient_backoff_ms=0)
    chunks = _collect(fb.stream(messages=[]))
    assert a.calls == 1 and b.calls == 1
    error_chunks = [c for c in chunks if c.type == ChunkType.ERROR]
    assert len(error_chunks) == 1
    assert "all 2" in error_chunks[0].error
    assert "b-fail" in error_chunks[0].error  # last error reported


def test_empty_chain_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty chain"):
        FallbackAdapter([], transient_retries=0, transient_backoff_ms=0)


def test_count_tokens_delegates_to_primary() -> None:
    class _CountingAdapter(_ScriptedAdapter):
        def count_tokens(self, messages):
            return 99

    primary = _CountingAdapter("primary")
    secondary = _ScriptedAdapter("secondary")
    fb = FallbackAdapter([(primary, _opts("p")), (secondary, _opts("s"))], transient_retries=0, transient_backoff_ms=0)
    assert fb.count_tokens([]) == 99


def test_last_used_options_tracks_failover_for_cost_ledger() -> None:
    """W3 reviewer follow-up (2026-04-29): cost ledger must bill failover
    spend to the model that actually streamed, not the primary's name."""
    primary = _ScriptedAdapter(
        "primary", chunks=[], raise_at=0, raise_exc=ConnectionError("503"),
    )
    secondary = _ScriptedAdapter("secondary", chunks=[
        StreamChunk(type=ChunkType.TEXT, text="ok"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ])
    fb = FallbackAdapter([(primary, _opts("primary-model")), (secondary, _opts("secondary-model"))], transient_retries=0, transient_backoff_ms=0)
    _collect(fb.stream(messages=[]))
    assert fb.last_used_options.model == "secondary-model"


def test_last_used_options_is_primary_when_no_failover() -> None:
    primary = _ScriptedAdapter("primary", chunks=[
        StreamChunk(type=ChunkType.TEXT, text="hi"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ])
    secondary = _ScriptedAdapter("secondary", chunks=[])
    fb = FallbackAdapter([(primary, _opts("primary-model")), (secondary, _opts("secondary-model"))], transient_retries=0, transient_backoff_ms=0)
    _collect(fb.stream(messages=[]))
    assert fb.last_used_options.model == "primary-model"


def test_reasoning_then_failure_still_falls_over() -> None:
    """REASONING_ITEM (Responses-API reasoning phase) must NOT count as
    commit. If primary streams a reasoning item then 5xxs before any
    TEXT/TOOL_CALL, fallback must still take over (W3 reviewer
    follow-up — premature commit edge case)."""
    primary = _ScriptedAdapter(
        "primary",
        chunks=[StreamChunk(type=ChunkType.REASONING_ITEM, text="thinking...")],
        raise_at=1,
        raise_exc=RuntimeError("upstream 503"),
    )
    secondary = _ScriptedAdapter("secondary", chunks=[
        StreamChunk(type=ChunkType.TEXT, text="from-secondary"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ])
    fb = FallbackAdapter([(primary, _opts("primary")), (secondary, _opts("secondary"))], transient_retries=0, transient_backoff_ms=0)
    chunks = _collect(fb.stream(messages=[]))
    assert primary.calls == 1
    assert secondary.calls == 1
    assert any(c.type == ChunkType.TEXT and c.text == "from-secondary" for c in chunks)
    assert not any(c.type == ChunkType.ERROR for c in chunks)


def test_reasoning_item_dropped_on_pre_commit_failover() -> None:
    """W3 reviewer follow-up C1 (2026-04-29): REASONING_ITEM chunks
    emitted by the primary BEFORE commit must not leak to the caller
    once failover happens. The primary's encrypted reasoning blob is
    bound to the primary's session — leaking it past failover would
    push a stale blob into history that the secondary cannot honor on
    the next tool-loop iteration."""
    primary = _ScriptedAdapter(
        "primary",
        chunks=[
            StreamChunk(type=ChunkType.REASONING_ITEM, raw={"item": {"id": "primary-reasoning"}}),
        ],
        raise_at=1,
        raise_exc=ConnectionError("upstream 503"),
    )
    secondary = _ScriptedAdapter("secondary", chunks=[
        StreamChunk(type=ChunkType.TEXT, text="clean"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ])
    fb = FallbackAdapter([(primary, _opts("primary")), (secondary, _opts("secondary"))], transient_retries=0, transient_backoff_ms=0)
    chunks = _collect(fb.stream(messages=[]))
    reasoning_chunks = [c for c in chunks if c.type == ChunkType.REASONING_ITEM]
    assert reasoning_chunks == [], (
        "primary's pre-commit REASONING_ITEM must be dropped on failover; "
        f"got {len(reasoning_chunks)} leaked chunks"
    )


def test_pre_commit_buffer_flushed_before_committed_chunk() -> None:
    """Pre-commit buffering must NOT swallow chunks on the success path.
    When primary commits, all buffered pre-commit chunks must flush in
    order BEFORE the first committed chunk."""
    primary = _ScriptedAdapter("primary", chunks=[
        StreamChunk(type=ChunkType.REASONING_ITEM, raw={"item": {"id": "ok-reasoning"}}),
        StreamChunk(type=ChunkType.TEXT, text="answer"),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ])
    secondary = _ScriptedAdapter("secondary", chunks=[])
    fb = FallbackAdapter([(primary, _opts("primary")), (secondary, _opts("secondary"))], transient_retries=0, transient_backoff_ms=0)
    chunks = _collect(fb.stream(messages=[]))
    # Reasoning must arrive before TEXT — caller-observed order
    # preserved.
    types = [c.type for c in chunks]
    assert ChunkType.REASONING_ITEM in types
    reasoning_idx = types.index(ChunkType.REASONING_ITEM)
    text_idx = types.index(ChunkType.TEXT)
    assert reasoning_idx < text_idx, f"buffer flush order broken: {types}"


def test_check_available_returns_true_if_any_reachable() -> None:
    class _UnavailableAdapter(_ScriptedAdapter):
        async def check_available(self):
            return False

    a = _UnavailableAdapter("a")
    b = _ScriptedAdapter("b")  # check_available -> True
    fb = FallbackAdapter([(a, _opts("a")), (b, _opts("b"))], transient_retries=0, transient_backoff_ms=0)
    assert asyncio.run(fb.check_available()) is True
