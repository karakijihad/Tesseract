"""Phase 2 (f) — observer counter routing.

The chip in Mirror reads `stats.fires_total` from `Observer.get_stats()`.
Before the fix, only `observe_incremental()` updated `_fires_total`, so
operators running stateless `/observe` (REPL or REST) saw "8 obs / 0 fires
/ 0 tok" because the counter never moved on the stateless path.

The fix unifies both paths so every successful model invocation bumps the
same counters, regardless of which entry point fired it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tesseract.brain.observer import Observer, ObserverConfig


class _StubAdapter:
    """Minimal adapter that returns a fixed text + token count via
    Observer._run_stream's expected interface."""


def _bare_observer(
    *,
    text: str = "test observation",
    tokens: int = 42,
    raise_budget: bool = False,
) -> Observer:
    obs = Observer.__new__(Observer)
    obs._adapter = None  # type: ignore[assignment]
    obs._config = ObserverConfig(
        provider="stub",
        model="stub-model",
        temperature=0.0,
        max_output_tokens=100,
        context_window=1024,
        timeout_seconds=10,
        max_retries=0,
    )
    obs._agent_def = None  # type: ignore[assignment]
    obs._cost_ledger = None
    from tesseract.brain.observation_transcript import ObservationTranscript
    from tesseract.brain.observer_budget import CircuitBreaker

    obs._transcript = ObservationTranscript()
    obs._circuit_breaker = CircuitBreaker()
    obs._fires_total = 0
    obs._tokens_used_total = 0
    obs._last_fired_at = None
    obs._last_suggestion_observation_id = None
    obs._lock = asyncio.Lock()

    async def _stub_run_stream(_messages: list[dict[str, Any]]) -> tuple[str, int]:
        if raise_budget:
            from tesseract.brain.cost import BudgetExhausted

            raise BudgetExhausted(
                role="observer_agent",
                spent_usd=1.0,
                cap_usd=1.0,
                scope="role",
            )
        return text, tokens

    def _stub_compose(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"role": "system", "content": "stub"}]

    obs._run_stream = _stub_run_stream  # type: ignore[assignment]
    obs._compose_messages = _stub_compose  # type: ignore[assignment]
    obs._compose_system_prompt = lambda *a, **kw: "stub"  # type: ignore[assignment]
    return obs


def test_stateless_observe_bumps_fires_counter(tmp_path, monkeypatch) -> None:
    """The stateless `observe()` path must increment `_fires_total` and
    `_tokens_used_total` — the chip reads these directly via `get_stats()`."""
    monkeypatch.setattr(
        "tesseract.brain.observer._append_observation_log",
        lambda **_kw: None,
    )
    obs = _bare_observer(text="anything", tokens=17)
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    asyncio.run(obs.observe(history))
    assert obs._fires_total == 1, "stateless observe() did not bump fires counter"
    assert obs._tokens_used_total == 17, "stateless observe() did not bump tokens counter"
    assert obs._last_fired_at is not None, "stateless observe() did not stamp last_fired_at"


def test_stateless_observe_stays_zero_on_budget_exhausted(tmp_path, monkeypatch) -> None:
    """Budget skip is not a fire — counter must stay put."""
    monkeypatch.setattr(
        "tesseract.brain.observer._append_observation_log",
        lambda **_kw: None,
    )
    obs = _bare_observer(raise_budget=True)
    history = [{"role": "user", "content": "ping"}]
    asyncio.run(obs.observe(history))
    assert obs._fires_total == 0
    assert obs._tokens_used_total == 0
    assert obs._last_fired_at is None


def test_get_stats_reflects_stateless_fires(tmp_path, monkeypatch) -> None:
    """End-to-end: get_stats() returns the bumped counters so the Mirror
    chip's "N obs / N tok / last fired" reading is non-zero after a
    stateless observation."""
    monkeypatch.setattr(
        "tesseract.brain.observer._append_observation_log",
        lambda **_kw: None,
    )
    obs = _bare_observer(text="emitted", tokens=23)
    history = [{"role": "user", "content": "anything"}]
    asyncio.run(obs.observe(history))
    asyncio.run(obs.observe(history))
    stats = obs.get_stats()
    assert stats["fires_total"] == 2
    assert stats["tokens_used_total"] == 46
    assert stats["last_fired_at"] is not None
