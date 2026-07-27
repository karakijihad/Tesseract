"""Workstream B — `FallbackAdapter` retries TRANSIENT pre-commit errors
against the same chain entry up to `transient_retries` times before
advancing. HARD pre-commit errors advance immediately. UNKNOWN is
treated as TRANSIENT (safe default).

Operator policy (2026-05-02): a transient throttle that should retry
the primary was instead advancing to a fallback model permanently for
the turn — wrong voice, wrong cost. The chain now distinguishes.
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


class _ProgrammableAdapter:
    """Yields a scripted sequence of chunks per call. `script` is a list
    of "scene" lists; each call to `stream()` consumes one scene and
    yields its chunks. After scenes are exhausted, raises."""

    def __init__(self, name: str, script: list[list[StreamChunk]]) -> None:
        self.model = name
        self._script = list(script)
        self.calls = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls += 1
        if not self._script:
            raise AssertionError(f"{self.model}: stream() called {self.calls}x — no scenes left")
        scene = self._script.pop(0)
        for chunk in scene:
            yield chunk

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _opts(model: str) -> AdapterOptions:
    return AdapterOptions(role="chat_brain", provider="test", model=model, tier="api")


def _err(text: str, kind: ErrorKind) -> StreamChunk:
    return StreamChunk(type=ChunkType.ERROR, error=text, error_kind=kind)


def _ok(text: str = "ok") -> list[StreamChunk]:
    return [
        StreamChunk(type=ChunkType.TEXT, text=text),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ]


@pytest.mark.asyncio
async def test_transient_retries_primary_n_times_then_advances() -> None:
    """Primary yields TRANSIENT 3 times (1 initial + 2 retries) → chain
    advances to secondary. Asserts retry budget is exactly consumed."""
    primary = _ProgrammableAdapter("primary", script=[
        [_err("503 upstream", ErrorKind.TRANSIENT)],
        [_err("503 upstream", ErrorKind.TRANSIENT)],
        [_err("503 upstream", ErrorKind.TRANSIENT)],
    ])
    secondary = _ProgrammableAdapter("secondary", script=[_ok("from secondary")])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=2,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 3, "primary must be tried 1+2=3 times before advancing"
    assert secondary.calls == 1
    assert any(c.type == ChunkType.TEXT and c.text == "from secondary" for c in chunks)

    # MODEL_SELECTED envelope must reflect the retries burnt.
    selects = [c for c in chunks if c.type == ChunkType.MODEL_SELECTED]
    assert len(selects) == 1
    raw = selects[0].raw or {}
    assert raw.get("is_fallback") is True
    assert raw.get("chain_index") == 1
    assert raw.get("transient_retries_exhausted") == 2


@pytest.mark.asyncio
async def test_hard_pre_commit_advances_immediately_no_retry() -> None:
    """HARD error → primary called exactly once, chain advances."""
    primary = _ProgrammableAdapter("primary", script=[
        [_err("401 invalid api key", ErrorKind.HARD)],
    ])
    secondary = _ProgrammableAdapter("secondary", script=[_ok("from secondary")])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=2,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 1, "HARD must not retry"
    assert secondary.calls == 1
    selects = [c for c in chunks if c.type == ChunkType.MODEL_SELECTED]
    assert len(selects) == 1
    assert (selects[0].raw or {}).get("transient_retries_exhausted") == 0


@pytest.mark.asyncio
async def test_primary_recovers_on_retry_secondary_never_invoked() -> None:
    """Primary fails TRANSIENT once, succeeds on retry → secondary stays
    cold and the committed text comes from primary."""
    primary = _ProgrammableAdapter("primary", script=[
        [_err("transient blip", ErrorKind.TRANSIENT)],
        _ok("from primary"),
    ])
    secondary = _ProgrammableAdapter("secondary", script=[_ok("must-not-run")])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=2,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 2
    assert secondary.calls == 0
    assert any(c.type == ChunkType.TEXT and c.text == "from primary" for c in chunks)

    # Primary recovered, but a retry was burnt — MODEL_SELECTED disclosed
    # because transient_attempts > 0 even though chain_index == 0.
    selects = [c for c in chunks if c.type == ChunkType.MODEL_SELECTED]
    assert len(selects) == 1
    raw = selects[0].raw or {}
    assert raw.get("is_fallback") is False, "primary recovered — not a fallback"
    assert raw.get("chain_index") == 0
    # Primary recovered after 1 retry; envelope discloses the burn so the
    # operator sees the cost of the recovery, not a misleading 0.
    assert raw.get("transient_retries_exhausted") == 1


@pytest.mark.asyncio
async def test_unknown_error_kind_treated_as_transient() -> None:
    """Adapter that emits ERROR without classifying (`error_kind=None`)
    should still be retried. Default-transient is the safe choice."""
    primary = _ProgrammableAdapter("primary", script=[
        [StreamChunk(type=ChunkType.ERROR, error="mysterious", error_kind=None)],
        _ok("recovered"),
    ])
    secondary = _ProgrammableAdapter("secondary", script=[_ok("must-not-run")])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=1,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 2
    assert secondary.calls == 0
    assert any(c.type == ChunkType.TEXT and c.text == "recovered" for c in chunks)


@pytest.mark.asyncio
async def test_chain_exhausted_yields_no_model_available() -> None:
    """All entries fail HARD → final ERROR chunk says no model
    available, with the last error reason embedded for triage."""
    a = _ProgrammableAdapter("a", script=[[_err("401 a-fail", ErrorKind.HARD)]])
    b = _ProgrammableAdapter("b", script=[[_err("404 b-fail", ErrorKind.HARD)]])
    fb = FallbackAdapter(
        [(a, _opts("a")), (b, _opts("b"))],
        transient_retries=2,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert a.calls == 1 and b.calls == 1, "HARD must not consume retry budget"
    err_chunks = [c for c in chunks if c.type == ChunkType.ERROR]
    assert len(err_chunks) == 1
    msg = err_chunks[0].error or ""
    assert "no chat_brain model available" in msg
    assert "all 2 chain entries exhausted" in msg
    assert "b-fail" in msg, "last error must surface for operator triage"
    assert err_chunks[0].error_kind == ErrorKind.HARD


@pytest.mark.asyncio
async def test_chain_exhausted_after_full_transient_budget() -> None:
    """Both entries TRANSIENT — each consumes 1+retries calls before
    advance — and final ERROR still surfaces."""
    a = _ProgrammableAdapter("a", script=[
        [_err("503", ErrorKind.TRANSIENT)],
        [_err("503", ErrorKind.TRANSIENT)],
    ])
    b = _ProgrammableAdapter("b", script=[
        [_err("503", ErrorKind.TRANSIENT)],
        [_err("503", ErrorKind.TRANSIENT)],
    ])
    fb = FallbackAdapter(
        [(a, _opts("a")), (b, _opts("b"))],
        transient_retries=1,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert a.calls == 2 and b.calls == 2
    err = next(c for c in chunks if c.type == ChunkType.ERROR)
    assert "no chat_brain model available" in (err.error or "")


@pytest.mark.asyncio
async def test_raised_exception_classified_and_retried() -> None:
    """A raised exception (not an ERROR chunk) is classified by
    `_exception_kind` — connection error → TRANSIENT → retried."""
    class _RaisingAdapter:
        def __init__(self) -> None:
            self.model = "raises"
            self.calls = 0

        async def stream(self, messages, tools=None, options=None):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("upstream dropped")
            yield StreamChunk(type=ChunkType.TEXT, text="recovered")
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")

        def count_tokens(self, messages):
            return 0

        async def check_available(self) -> bool:
            return True

    primary = _RaisingAdapter()
    secondary = _ProgrammableAdapter("secondary", script=[_ok("nope")])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=1,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 2
    assert secondary.calls == 0
    assert any(c.type == ChunkType.TEXT and c.text == "recovered" for c in chunks)


@pytest.mark.asyncio
async def test_zero_retries_advances_after_first_failure() -> None:
    """`transient_retries=0` → no retry; advance on first TRANSIENT."""
    primary = _ProgrammableAdapter("primary", script=[
        [_err("503", ErrorKind.TRANSIENT)],
    ])
    secondary = _ProgrammableAdapter("secondary", script=[_ok("from secondary")])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=0,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 1
    assert secondary.calls == 1
    assert any(c.type == ChunkType.TEXT and c.text == "from secondary" for c in chunks)


@pytest.mark.asyncio
async def test_negative_config_rejected() -> None:
    """Invalid retry / backoff values fail loudly at construction."""
    with pytest.raises(ValueError, match="transient_retries"):
        FallbackAdapter(
            [(_ProgrammableAdapter("p", script=[]), _opts("p"))],
            transient_retries=-1,
            transient_backoff_ms=0,
        )
    with pytest.raises(ValueError, match="transient_backoff_ms"):
        FallbackAdapter(
            [(_ProgrammableAdapter("p", script=[]), _opts("p"))],
            transient_retries=0,
            transient_backoff_ms=-1,
        )
