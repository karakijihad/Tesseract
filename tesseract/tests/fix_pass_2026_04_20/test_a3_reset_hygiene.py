"""A3 — reset/disarm leaves stale observer + chat-side state.

Codex #3 + Claude reviewer M-1:
- Observer.reset() clears transcript only; leaves circuit_breaker and
  _last_suggestion_observation_id intact.
- ChatSession.reset() clears history + _observer_last_index only; leaves
  _pending_suggestions, _observed_ids, _turn_injection intact.

This means a disarm→rearm cycle replays stale suggestions and keeps the
breaker tripped against fresh sessions.
"""

from __future__ import annotations

from tesseract.brain.chat import ChatSession
from tesseract.brain.memory_suggestion import MemoryPath, MemorySuggestion
from tesseract.brain.observer import Observer, ObserverConfig
from tesseract.brain.observer_budget import CircuitBreaker
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter


class _Dummy:
    pass


def test_observer_reset() -> None:
    # Construct just the stateful bits; we're not streaming.
    obs = Observer.__new__(Observer)
    obs._adapter = None  # type: ignore[assignment]
    obs._config = None  # type: ignore[assignment]
    obs._agent_def = None  # type: ignore[assignment]
    from tesseract.brain.observation_transcript import ObservationTranscript

    obs._transcript = ObservationTranscript()
    obs._circuit_breaker = CircuitBreaker(max_consecutive_failures=2, cooldown_seconds=60)
    obs._fires_total = 0
    obs._tokens_used_total = 0
    obs._last_fired_at = None
    obs._last_suggestion_observation_id = "obs_prev_abcd"
    # Trip the breaker.
    obs._circuit_breaker.record_failure()
    obs._circuit_breaker.record_failure()
    assert obs._circuit_breaker.is_open()

    obs.reset()

    assert not obs._circuit_breaker.is_open(), "BUG: breaker still open after reset"
    assert obs._last_suggestion_observation_id is None, (
        f"BUG: _last_suggestion_observation_id={obs._last_suggestion_observation_id!r} survived reset"
    )
    assert len(obs._transcript.chat_turns) == 0


def test_chat_reset() -> None:
    cs = ChatSession(
        adapter=None,  # type: ignore[arg-type]
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
    )
    # Seed stale state as if the session had observer activity.
    cs.history = [{"role": "user", "content": "x"}]
    cs._observer_last_index = 1
    cs._turn_injection = "leftover_injection_text"
    stale = MemorySuggestion(
        kind="remember",
        target=MemoryPath(path="stale.md"),
        reason="stale",
        confidence=0.5,
        observation_id="obs_stale",
    )
    cs._pending_suggestions.append(stale)
    cs._observed_ids.append("obs_stale")

    cs.reset()

    assert cs.history == [], "history not cleared"
    assert cs._observer_last_index == 0, "watermark not cleared"
    assert cs._turn_injection == "", f"BUG: _turn_injection={cs._turn_injection!r} survived reset"
    assert len(cs._pending_suggestions) == 0, (
        f"BUG: _pending_suggestions has {len(cs._pending_suggestions)} entries after reset"
    )
    assert len(cs._observed_ids) == 0, (
        f"BUG: _observed_ids has {len(cs._observed_ids)} entries after reset — future re-injection of same id will be silently dropped"
    )
