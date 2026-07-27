"""Observer-side ledger wiring: sub-cap fires before chat's does."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import MagicMock

import pytest
import yaml

from tesseract.agents.loader import AgentDefinition
from tesseract.brain.cost import CostLedger
from tesseract.brain.observer import Observer, ObserverConfig
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


class _FakeAdapter(ModelAdapter):
    def __init__(self, text: str, usage: dict) -> None:
        self.text = text
        self.usage = usage
        self.calls = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls += 1
        yield StreamChunk(type=ChunkType.TEXT, text=self.text)
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end", raw={"usage": self.usage})

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _make_agent_def() -> AgentDefinition:
    """A minimal AgentDefinition with the observation + suggestion prompts."""
    agent = MagicMock(spec=AgentDefinition)
    agent.name = "observer"
    agent.get_section = MagicMock(return_value="{transcript}\n\n{pty_context}")
    return agent


def _models_yaml(tmp_path: Path, per_role: dict, cap_usd: float = 10.0, warn_usd: float = 5.0) -> Path:
    # cap_usd and warn_usd are kept as parameters for call-site compatibility.
    # Under the new ledger contract, cap_usd is derived from per_role + voice
    # caps. We override per_role to include an extra "budget_headroom" entry
    # so the derived global cap >= the caller-specified cap_usd, unless the
    # caller's per_role already sums to >= cap_usd.
    pr_sum = sum(per_role.values())
    extra = max(0.0, cap_usd - pr_sum)
    effective_per_role = dict(per_role)
    if extra > 0:
        effective_per_role["_headroom"] = extra
    warn_pct = round(warn_usd / cap_usd, 10) if cap_usd > 0 else 0.75
    data = {
        "providers": {"openai": {"timeout_seconds": 60, "max_retries": 3}},
        "roles": {
            "chat_brain": {
                "resolution": [
                    {"model": "gpt-5.4-nano", "cost_per_mtok_in": 0.20, "cost_per_mtok_out": 1.25}
                ]
            },
            "observer_agent": {
                "resolution": [
                    {"model": "gpt-5.4-nano", "cost_per_mtok_in": 0.20, "cost_per_mtok_out": 1.25}
                ]
            },
        },
        "cost_tracking": {
            "enabled": True,
            "warning_at_pct": warn_pct,
            "log_file": "logs/cost-tracking.jsonl",
            "per_role": effective_per_role,
        },
    }
    target = tmp_path / "models.yaml"
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


def _seed_spent(log_path: Path, role: str, cost_usd: float, local_date: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": f"{local_date}T12:00:00Z",
            "local_date": local_date,
            "role": role,
            "model": "gpt-5.4-nano",
            "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
            "cost_usd": cost_usd, "daily_total_usd": cost_usd, "role_total_usd": cost_usd,
        }) + "\n")


def _build_observer(ledger: CostLedger, adapter: ModelAdapter) -> Observer:
    config = ObserverConfig(
        model="gpt-5.4-nano",
        provider="openai",
        temperature=1.0,
        max_output_tokens=2048,
        context_window=400_000,
        timeout_seconds=60,
        max_retries=3,
        reasoning_effort="low",
        use_responses_api=True,
    )
    return Observer(adapter=adapter, config=config, agent_def=_make_agent_def(), cost_ledger=ledger)


async def test_observer_skipped_when_subcap_hit(tmp_path: Path) -> None:
    log_path = tmp_path / "cost.jsonl"
    models_yaml = _models_yaml(tmp_path, per_role={"observer_agent": 0.10, "chat_brain": 2.50})
    _seed_spent(log_path, "observer_agent", 0.20, "2026-04-24")

    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-24"
    )

    adapter = _FakeAdapter(text="something", usage={"input_tokens": 100, "output_tokens": 50})
    observer = _build_observer(ledger, adapter)

    out = await observer.observe([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])

    assert out == ""
    assert adapter.calls == 0, "adapter must not be called when observer sub-cap is hit"


async def test_observer_records_against_observer_role_not_chat(tmp_path: Path) -> None:
    log_path = tmp_path / "cost.jsonl"
    models_yaml = _models_yaml(tmp_path, per_role={"observer_agent": 0.50, "chat_brain": 2.50})

    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-24"
    )

    adapter = _FakeAdapter(
        text="something worth noting",
        usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000},
    )
    observer = _build_observer(ledger, adapter)

    await observer.observe([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])

    # Observer spend lands on observer_agent, not chat_brain.
    assert ledger.budget_state("observer_agent").role_spent_usd == pytest.approx(1.45, abs=1e-9)
    assert ledger.budget_state("chat_brain").role_spent_usd == 0.0


async def test_budget_skip_does_not_trip_circuit_breaker(tmp_path: Path) -> None:
    """Budget-skipped observations must not count as adapter failures.

    Regression guard: an earlier version of `_run_stream` returned `(None, 0)`
    on BudgetExhausted, which `observe_incremental` interpreted as adapter
    failure and forwarded to `CircuitBreaker.record_failure()`. Three budget
    skips would lock the observer out for 60 s even after the daily ledger
    rolled over at midnight. Fix: let BudgetExhausted propagate; callers
    handle it without touching the breaker or fires counter.
    """
    log_path = tmp_path / "cost.jsonl"
    models_yaml = _models_yaml(tmp_path, per_role={"observer_agent": 0.10, "chat_brain": 2.50})
    _seed_spent(log_path, "observer_agent", 0.20, "2026-04-24")

    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-24"
    )

    adapter = _FakeAdapter(text="x", usage={"input_tokens": 1, "output_tokens": 1})
    observer = _build_observer(ledger, adapter)

    # Ten budget-skipped calls — well past the breaker's 3-failure threshold.
    for _ in range(10):
        result = await observer.observe_incremental([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        assert result is None

    # Breaker is still closed; fires counter did not move.
    stats = observer.get_stats()
    assert stats["circuit_breaker_state"] == "green"
    assert stats["fires_total"] == 0
    assert stats["tokens_used_total"] == 0
    assert adapter.calls == 0


async def test_chat_still_runs_when_observer_subcap_hit(tmp_path: Path) -> None:
    """Observer can be budget-exhausted while chat_brain remains available."""
    log_path = tmp_path / "cost.jsonl"
    models_yaml = _models_yaml(
        tmp_path, per_role={"observer_agent": 0.10, "chat_brain": 2.50}, cap_usd=5.0
    )
    _seed_spent(log_path, "observer_agent", 0.25, "2026-04-24")

    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-24"
    )

    # Observer hits its sub-cap → raises internally, returns None/empty.
    observer = _build_observer(
        ledger, _FakeAdapter("x", usage={"input_tokens": 1, "output_tokens": 1})
    )
    assert await observer.observe([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]) == ""

    # chat_brain preflight still passes (0 spend on chat_brain role).
    ledger.check_preflight("chat_brain")
