"""Per-chain-entry cooldown breaker.

Operator directive (2026-05-02): if a chain entry advances ("one
failed"), the chain should skip it for ~n minutes before trying again,
falling over to the next entry instead of repeatedly burning retry
budget against a wedged provider. Cooldown elapses → next attempt is a
half-open probe; success closes the breaker, failure restarts the
cooldown.

Globals come from `providers.yaml::chain.cooldown_max_failures` /
`cooldown_seconds`; per-provider override via the same keys on the
provider's connection block (loader → `AdapterOptions.extra` →
`FallbackAdapter._build_breaker`).
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


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


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


def _opts(model: str, *, extra: dict[str, Any] | None = None) -> AdapterOptions:
    return AdapterOptions(
        role="chat_brain", provider="test", model=model, tier="api",
        extra=extra or {},
    )


def _err(text: str, kind: ErrorKind) -> StreamChunk:
    return StreamChunk(type=ChunkType.ERROR, error=text, error_kind=kind)


def _ok(text: str = "ok") -> list[StreamChunk]:
    return [
        StreamChunk(type=ChunkType.TEXT, text=text),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ]


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_failures_and_skips_entry() -> None:
    """Two consecutive advances on primary → breaker opens → third
    turn skips primary entirely and falls through to secondary."""
    clock = _FakeClock()
    primary = _Programmable("primary", script=[
        [_err("503", ErrorKind.HARD)],   # turn 1: advance
        [_err("503", ErrorKind.HARD)],   # turn 2: advance, breaker opens
        # No script entry for turn 3 — primary must be skipped.
    ])
    secondary = _Programmable("secondary", script=[
        _ok("turn1"),
        _ok("turn2"),
        _ok("turn3"),
    ])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=0,
        transient_backoff_ms=0,
        cooldown_max_failures=2,
        cooldown_seconds=60.0,
        time_func=clock,
    )

    for _ in range(3):
        chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]
        assert any(c.type == ChunkType.TEXT for c in chunks)

    assert primary.calls == 2, "primary must be skipped on turn 3 (breaker open)"
    assert secondary.calls == 3


@pytest.mark.asyncio
async def test_breaker_closes_after_cooldown_expires_and_probe_succeeds() -> None:
    """Open breaker + cooldown elapsed + primary recovers → breaker
    closes, subsequent turns hit primary again."""
    clock = _FakeClock()
    primary = _Programmable("primary", script=[
        [_err("503", ErrorKind.HARD)],   # turn 1: advance, breaker opens
        _ok("recovered"),                # turn 2 (after cooldown): success
        _ok("steady"),                   # turn 3: still healthy
    ])
    secondary = _Programmable("secondary", script=[
        _ok("turn1-fallback"),
    ])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=0,
        transient_backoff_ms=0,
        cooldown_max_failures=1,
        cooldown_seconds=60.0,
        time_func=clock,
    )

    # Turn 1 — primary fails, secondary serves.
    [c async for c in fb.stream(messages=[], tools=None, options=None)]
    assert primary.calls == 1
    assert secondary.calls == 1

    # Cooldown active — without advancing the clock, primary stays skipped.
    # Advance past cooldown.
    clock.advance(61.0)

    # Turn 2 — primary probed, succeeds, breaker closes.
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]
    assert primary.calls == 2
    assert secondary.calls == 1, "secondary must not be touched on turn 2"
    assert any(c.type == ChunkType.TEXT and c.text == "recovered" for c in chunks)

    # Turn 3 — breaker is closed; primary still served.
    [c async for c in fb.stream(messages=[], tools=None, options=None)]
    assert primary.calls == 3


@pytest.mark.asyncio
async def test_failed_probe_restarts_cooldown() -> None:
    """Cooldown elapses → probe fails → breaker re-opens for another
    full cooldown window (not a stuck-open state, not an instant-retry)."""
    clock = _FakeClock()
    primary = _Programmable("primary", script=[
        [_err("503", ErrorKind.HARD)],   # turn 1: advance → opens
        [_err("503", ErrorKind.HARD)],   # turn 2 probe: fails → re-opens
        # No turn 3 entry — must be skipped (re-opened).
    ])
    secondary = _Programmable("secondary", script=[
        _ok("t1"), _ok("t2"), _ok("t3"),
    ])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=0,
        transient_backoff_ms=0,
        cooldown_max_failures=1,
        cooldown_seconds=60.0,
        time_func=clock,
    )

    [c async for c in fb.stream(messages=[], tools=None, options=None)]
    clock.advance(61.0)
    [c async for c in fb.stream(messages=[], tools=None, options=None)]
    # Don't advance — turn 3 should still be inside the new cooldown.
    [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 2, "primary must be skipped on turn 3 (re-opened)"
    assert secondary.calls == 3


@pytest.mark.asyncio
async def test_per_provider_cooldown_override_via_extra() -> None:
    """Provider sets `chain_cooldown_seconds=10` in extra → its breaker
    uses 10s, not the global 60s."""
    clock = _FakeClock()
    primary = _Programmable("primary", script=[
        [_err("503", ErrorKind.HARD)],
        _ok("recovered"),
    ])
    secondary = _Programmable("secondary", script=[_ok("from-secondary")])
    fb = FallbackAdapter(
        [
            (primary, _opts("primary", extra={"chain_cooldown_seconds": 10.0})),
            (secondary, _opts("secondary")),
        ],
        transient_retries=0,
        transient_backoff_ms=0,
        cooldown_max_failures=1,
        cooldown_seconds=60.0,  # global big — per-entry override is small
        time_func=clock,
    )

    [c async for c in fb.stream(messages=[], tools=None, options=None)]
    # Global says 60s, but this entry was overridden to 10s.
    clock.advance(11.0)
    [c async for c in fb.stream(messages=[], tools=None, options=None)]
    assert primary.calls == 2, "10s override must let primary be tried again"


@pytest.mark.asyncio
async def test_disabled_breaker_never_opens() -> None:
    """Both knobs at 0 (or either) → breaker is disabled. Repeated
    failures never skip the entry."""
    clock = _FakeClock()
    primary = _Programmable("primary", script=[
        [_err("503", ErrorKind.HARD)],
        [_err("503", ErrorKind.HARD)],
        [_err("503", ErrorKind.HARD)],
    ])
    secondary = _Programmable("secondary", script=[
        _ok("t1"), _ok("t2"), _ok("t3"),
    ])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=0,
        transient_backoff_ms=0,
        cooldown_max_failures=0,  # disabled
        cooldown_seconds=60.0,
        time_func=clock,
    )

    for _ in range(3):
        [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 3, "disabled breaker → primary tried every turn"
    assert secondary.calls == 3


@pytest.mark.asyncio
async def test_all_entries_in_cooldown_emits_clear_error() -> None:
    """When every entry is in cooldown, the operator-facing ERROR says
    so explicitly — TARS retries on the next turn instead of looping."""
    clock = _FakeClock()
    primary = _Programmable("primary", script=[[_err("p", ErrorKind.HARD)]])
    secondary = _Programmable("secondary", script=[[_err("s", ErrorKind.HARD)]])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=0,
        transient_backoff_ms=0,
        cooldown_max_failures=1,
        cooldown_seconds=60.0,
        time_func=clock,
    )

    # Turn 1 — both fail, both breakers open.
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]
    err = next(c for c in chunks if c.type == ChunkType.ERROR)
    assert err.error_kind == ErrorKind.HARD

    # Turn 2 — both in cooldown → "all entries cooling down".
    chunks = [c async for c in fb.stream(messages=[], tools=None, options=None)]
    err = next(c for c in chunks if c.type == ChunkType.ERROR)
    assert "cooling down" in (err.error or "")
    # Neither adapter touched on turn 2.
    assert primary.calls == 1
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_success_resets_failure_counter() -> None:
    """A successful turn between failures resets the breaker's
    consecutive-failure count — only *consecutive* advances trip it."""
    clock = _FakeClock()
    primary = _Programmable("primary", script=[
        [_err("503", ErrorKind.HARD)],   # advance (count=1)
        _ok("ok"),                       # success → counter resets
        [_err("503", ErrorKind.HARD)],   # advance (count=1, NOT 2 → no trip)
        _ok("ok"),                       # success
    ])
    secondary = _Programmable("secondary", script=[
        _ok("s1"), _ok("s2"),
    ])
    fb = FallbackAdapter(
        [(primary, _opts("primary")), (secondary, _opts("secondary"))],
        transient_retries=0,
        transient_backoff_ms=0,
        cooldown_max_failures=2,  # need 2 consecutive
        cooldown_seconds=60.0,
        time_func=clock,
    )

    for _ in range(4):
        [c async for c in fb.stream(messages=[], tools=None, options=None)]

    assert primary.calls == 4, "non-consecutive failures must not open breaker"
    assert secondary.calls == 2


@pytest.mark.asyncio
async def test_negative_cooldown_config_rejected() -> None:
    fake = _Programmable("p", script=[])
    with pytest.raises(ValueError):
        FallbackAdapter(
            [(fake, _opts("p"))],
            transient_retries=0,
            transient_backoff_ms=0,
            cooldown_max_failures=-1,
            cooldown_seconds=60.0,
        )
    with pytest.raises(ValueError):
        FallbackAdapter(
            [(fake, _opts("p"))],
            transient_retries=0,
            transient_backoff_ms=0,
            cooldown_max_failures=1,
            cooldown_seconds=-1.0,
        )
