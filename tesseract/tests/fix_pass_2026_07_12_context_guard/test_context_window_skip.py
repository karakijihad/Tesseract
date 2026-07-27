"""Context-window guard on the fallback chain.

2026-07-12 incident: a ~253k-token history was streamed to
gpt-oss-120b (131072-token window). NIM computes
``max_tokens = window - prompt`` server-side, got -122002, and 400'd —
burning the entry, tripping its breaker, and round-tripping a request
that could never succeed. The chain must skip entries whose declared
``context_window`` cannot hold the prompt estimate, without recording
a breaker failure (the provider isn't at fault).
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


class _Programmable:
    def __init__(self, name: str, script: list[list[StreamChunk]], tokens: int = 0) -> None:
        self.model = name
        self._script = list(script)
        self._tokens = tokens
        self.calls = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls += 1
        if not self._script:
            raise AssertionError(f"{self.model}: ran out of scenes at call {self.calls}")
        for chunk in self._script.pop(0):
            yield chunk

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return self._tokens

    async def check_available(self) -> bool:
        return True


def _opts(model: str, *, context_window: int | None = 1_000_000) -> AdapterOptions:
    return AdapterOptions(
        role="chat_brain", provider="test", model=model, tier="api",
        context_window=context_window,  # type: ignore[arg-type]
    )


def _ok(text: str = "ok") -> list[StreamChunk]:
    return [
        StreamChunk(type=ChunkType.TEXT, text=text),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ]


def _fb(chain: list[tuple[_Programmable, AdapterOptions]]) -> FallbackAdapter:
    return FallbackAdapter(
        chain, transient_retries=0, transient_backoff_ms=0,
        cooldown_max_failures=2, cooldown_seconds=60.0,
    )


@pytest.mark.asyncio
async def test_oversized_prompt_skips_small_window_entry() -> None:
    """Prompt estimate over the primary's window → primary never called,
    secondary commits and is disclosed as the fallback."""
    primary = _Programmable("small-window", script=[], tokens=253_074)
    secondary = _Programmable("big-window", script=[_ok()], tokens=253_074)
    fb = _fb([
        (primary, _opts("small-window", context_window=131_072)),
        (secondary, _opts("big-window", context_window=1_000_000)),
    ])

    chunks = [c async for c in fb.stream(messages=[{"role": "user", "content": "x"}])]

    assert primary.calls == 0
    assert secondary.calls == 1
    selected = [c for c in chunks if c.type == ChunkType.MODEL_SELECTED]
    assert selected and selected[0].raw["is_fallback"] is True
    assert "context" in selected[0].raw["fallback_reason"]


@pytest.mark.asyncio
async def test_fitting_prompt_uses_primary() -> None:
    primary = _Programmable("primary", script=[_ok()], tokens=100_000)
    secondary = _Programmable("secondary", script=[], tokens=100_000)
    fb = _fb([
        (primary, _opts("primary", context_window=131_072)),
        (secondary, _opts("secondary", context_window=1_000_000)),
    ])

    chunks = [c async for c in fb.stream(messages=[{"role": "user", "content": "x"}])]

    assert primary.calls == 1
    assert secondary.calls == 0
    assert any(c.type == ChunkType.TEXT for c in chunks)


@pytest.mark.asyncio
async def test_unknown_context_window_never_skips() -> None:
    """role_chain builds options with context_window=None when the catalog
    entry lacks one — unknown window must not trigger the guard."""
    primary = _Programmable("no-window", script=[_ok()], tokens=253_074)
    fb = _fb([(primary, _opts("no-window", context_window=None))])

    chunks = [c async for c in fb.stream(messages=[{"role": "user", "content": "x"}])]

    assert primary.calls == 1
    assert any(c.type == ChunkType.TEXT for c in chunks)


@pytest.mark.asyncio
async def test_size_skip_does_not_trip_breaker() -> None:
    """A size-skip is not a provider failure: after two skipped turns the
    breaker stays closed and a fitting prompt reaches the entry."""
    primary = _Programmable("primary", script=[_ok()], tokens=0)
    secondary = _Programmable("secondary", script=[_ok(), _ok()], tokens=0)

    def tokens(n: int) -> None:
        primary._tokens = n
        secondary._tokens = n

    fb = _fb([
        (primary, _opts("primary", context_window=1_000)),
        (secondary, _opts("secondary", context_window=1_000_000)),
    ])

    tokens(5_000)
    for _ in range(2):
        _ = [c async for c in fb.stream(messages=[{"role": "user", "content": "x"}])]
    assert primary.calls == 0

    tokens(100)
    _ = [c async for c in fb.stream(messages=[{"role": "user", "content": "x"}])]
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_all_entries_skipped_yields_hard_error() -> None:
    a = _Programmable("a", script=[], tokens=5_000)
    b = _Programmable("b", script=[], tokens=5_000)
    fb = _fb([
        (a, _opts("a", context_window=1_000)),
        (b, _opts("b", context_window=2_000)),
    ])

    chunks = [c async for c in fb.stream(messages=[{"role": "user", "content": "x"}])]

    assert a.calls == 0 and b.calls == 0
    errors = [c for c in chunks if c.type == ChunkType.ERROR]
    assert errors and errors[-1].error_kind == ErrorKind.HARD
    assert "context" in (errors[-1].error or "")
