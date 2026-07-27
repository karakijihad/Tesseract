"""Per-provider override of the chain retry policy.

Each chain entry may diverge from the global `chain.transient_retries`
/ `transient_backoff_ms` by setting its own value on the provider's
connection block. The override rides along on `AdapterOptions.extra`
under `chain_transient_retries` / `chain_transient_backoff_ms`. Set in
`boot.adapter_options_from_chat_brain`; consumed in
`adapter_chain.FallbackAdapter.stream`.

Operator motivation (2026-05-02): an Anthropic 529 overload can wedge
for minutes — retrying twice burns ~750ms before falling over. With
`api.anthropic.transient_retries: 0` the chain advances instantly to
Gemini. Conversely OpenAI 5xx blips usually clear in <1s, so a
provider-specific bump to `3` is fine.
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
            raise AssertionError(f"{self.model}: ran out of scenes at call {self.calls}")
        for chunk in self._script.pop(0):
            yield chunk

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _opts(model: str, *, retries: int | None = None, backoff_ms: int | None = None) -> AdapterOptions:
    extra: dict[str, Any] = {}
    if retries is not None:
        extra["chain_transient_retries"] = retries
    if backoff_ms is not None:
        extra["chain_transient_backoff_ms"] = backoff_ms
    return AdapterOptions(role="chat_brain", provider="test", model=model, tier="api", extra=extra)


def _err(text: str, kind: ErrorKind) -> StreamChunk:
    return StreamChunk(type=ChunkType.ERROR, error=text, error_kind=kind)


def _ok(text: str = "ok") -> list[StreamChunk]:
    return [
        StreamChunk(type=ChunkType.TEXT, text=text),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ]


@pytest.mark.asyncio
async def test_entry_with_zero_retries_advances_immediately_despite_global_two() -> None:
    """Global says retry 2x; this entry's override says 0 → advance on
    first transient failure. Models the Anthropic-overload case."""
    primary = _Programmable("anthropic", script=[
        [_err("529 overloaded", ErrorKind.TRANSIENT)],
    ])
    secondary = _Programmable("gemini", script=[_ok("from gemini")])
    fb = FallbackAdapter(
        [
            (primary, _opts("anthropic", retries=0)),
            (secondary, _opts("gemini")),
        ],
        transient_retries=2,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 1, "override 0 must skip retries"
    assert secondary.calls == 1
    assert any(c.type == ChunkType.TEXT and c.text == "from gemini" for c in chunks)


@pytest.mark.asyncio
async def test_entry_with_higher_retry_budget_overrides_global() -> None:
    """Global says retry 1x; this entry's override says 3 → primary
    gets 1 + 3 = 4 calls before advance. Models the bumpy-OpenAI case."""
    primary = _Programmable("openai", script=[
        [_err("503", ErrorKind.TRANSIENT)],
        [_err("503", ErrorKind.TRANSIENT)],
        [_err("503", ErrorKind.TRANSIENT)],
        [_err("503", ErrorKind.TRANSIENT)],
    ])
    secondary = _Programmable("gemini", script=[_ok("from gemini")])
    fb = FallbackAdapter(
        [
            (primary, _opts("openai", retries=3)),
            (secondary, _opts("gemini")),
        ],
        transient_retries=1,  # lower global; per-entry must win
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 4
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_missing_override_inherits_global() -> None:
    """No `extra` keys → use the global from constructor kwargs."""
    primary = _Programmable("primary", script=[
        [_err("503", ErrorKind.TRANSIENT)],
        [_err("503", ErrorKind.TRANSIENT)],
        [_err("503", ErrorKind.TRANSIENT)],
    ])
    secondary = _Programmable("secondary", script=[_ok("from secondary")])
    fb = FallbackAdapter(
        [
            (primary, _opts("primary")),
            (secondary, _opts("secondary")),
        ],
        transient_retries=2,
        transient_backoff_ms=0,
    )
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]

    # Inherits global → 1 + 2 retries = 3 calls before advance.
    assert primary.calls == 3
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_per_entry_overrides_independent_per_entry() -> None:
    """Three-entry chain — each with its own retry budget — confirms
    overrides are read on the entry being attempted, not the primary."""
    a = _Programmable("a", script=[
        [_err("503", ErrorKind.TRANSIENT)],  # only 1 call (override 0)
    ])
    b = _Programmable("b", script=[
        [_err("503", ErrorKind.TRANSIENT)],  # 3 calls (override 2)
        [_err("503", ErrorKind.TRANSIENT)],
        [_err("503", ErrorKind.TRANSIENT)],
    ])
    c = _Programmable("c", script=[_ok("from c")])
    fb = FallbackAdapter(
        [
            (a, _opts("a", retries=0)),
            (b, _opts("b", retries=2)),
            (c, _opts("c")),
        ],
        transient_retries=99,  # big global; per-entry must win
        transient_backoff_ms=0,
    )
    chunks = [_ async for _ in fb.stream(messages=[], tools=None, options=None)]

    assert a.calls == 1
    assert b.calls == 3
    assert c.calls == 1
    assert any(ch.type == ChunkType.TEXT and ch.text == "from c" for ch in chunks)


@pytest.mark.asyncio
async def test_per_entry_backoff_override_respected() -> None:
    """Override `chain_transient_backoff_ms=0` on a slow-default chain
    so this entry retries without sleeping. Catches the case where
    `_sleep_backoff` ignored the per-entry override."""
    import time

    primary = _Programmable("p", script=[
        [_err("503", ErrorKind.TRANSIENT)],
        _ok("recovered"),
    ])
    secondary = _Programmable("s", script=[_ok("must-not-run")])
    fb = FallbackAdapter(
        [
            (primary, _opts("p", retries=1, backoff_ms=0)),
            (secondary, _opts("s")),
        ],
        transient_retries=0,        # global retries 0 — but entry overrides to 1
        transient_backoff_ms=10000, # global 10s — but entry overrides to 0
    )
    t0 = time.monotonic()
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]
    elapsed_s = time.monotonic() - t0

    assert primary.calls == 2
    assert secondary.calls == 0
    assert elapsed_s < 1.0, f"per-entry backoff override ignored — slept {elapsed_s:.2f}s"
    assert any(c.type == ChunkType.TEXT and c.text == "recovered" for c in chunks)
