"""Pricing math: per-MTok rates, cached-input discount, unknown-role guard."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tesseract.brain.cost import BudgetExhausted, CostLedger, CostUsage
from tesseract.brain.cost.ledger import CACHED_INPUT_RATE


def _write_yaml(tmp_path: Path, cost_tracking: dict, roles: dict) -> Path:
    """Write a minimal models.yaml under tmp_path and return its path."""
    data = {
        "providers": {"openai": {"timeout_seconds": 60, "max_retries": 3}},
        "roles": roles,
        "cost_tracking": cost_tracking,
    }
    target = tmp_path / "models.yaml"
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


def _basic_yaml(tmp_path: Path, enabled: bool = True, per_role: dict | None = None) -> tuple[Path, Path]:
    log_path = tmp_path / "cost.jsonl"
    models_yaml = _write_yaml(
        tmp_path,
        cost_tracking={
            "enabled": enabled,
            "warning_at_pct": 0.75,
            "log_file": "logs/cost-tracking.jsonl",
            **({"per_role": per_role} if per_role else {}),
        },
        roles={
            "chat_brain": {
                "resolution": [
                    {
                        "model": "gpt-5.4-nano",
                        "cost_per_mtok_in": 0.20,
                        "cost_per_mtok_out": 1.25,
                    }
                ]
            },
            "observer_agent": {
                "resolution": [
                    {
                        "model": "gpt-5.4-nano",
                        "cost_per_mtok_in": 0.20,
                        "cost_per_mtok_out": 1.25,
                    }
                ]
            },
            "claude_cli": {
                "resolution": [
                    {"model": "claude-opus-4-7", "cost_per_mtok_in": 0, "cost_per_mtok_out": 0}
                ]
            },
        },
    )
    return models_yaml, log_path


async def test_pricing_lookup_uncached_input_and_output(tmp_path: Path) -> None:
    models_yaml, log_path = _basic_yaml(tmp_path)
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    event = ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=0),
    )

    # 1M input @ $0.20 + 1M output @ $1.25 = $1.45
    assert event.cost_usd == pytest.approx(1.45, abs=1e-9)
    assert event.daily_total_usd == pytest.approx(1.45, abs=1e-9)
    assert event.role_total_usd == pytest.approx(1.45, abs=1e-9)


async def test_cached_tokens_get_90_percent_discount(tmp_path: Path) -> None:
    models_yaml, log_path = _basic_yaml(tmp_path)
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    # 1M input total, 800k of which is cached. Output = 0 to isolate the input math.
    event = ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=1_000_000, output_tokens=0, cached_tokens=800_000),
    )

    # uncached 200k @ $0.20 = $0.04
    # cached 800k @ $0.20 * 0.1 = $0.016
    # total = $0.056
    assert CACHED_INPUT_RATE == pytest.approx(0.1)
    expected = (200_000 * 0.20 + 800_000 * 0.20 * 0.1) / 1_000_000
    assert event.cost_usd == pytest.approx(expected, abs=1e-9)


async def test_unknown_role_model_raises(tmp_path: Path) -> None:
    models_yaml, log_path = _basic_yaml(tmp_path)
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    with pytest.raises(RuntimeError, match="no pricing for model=unknown"):
        ledger.record("chat_brain", "unknown", CostUsage(input_tokens=100, output_tokens=100))


async def test_cli_zero_priced_model_records_zero_cost(tmp_path: Path) -> None:
    """CLI roles with cost_per_mtok_*: 0 still produce an event, priced at $0."""
    models_yaml, log_path = _basic_yaml(tmp_path)
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    event = ledger.record(
        "claude_cli",
        "claude-opus-4-7",
        CostUsage(input_tokens=100_000, output_tokens=100_000),
    )
    assert event.cost_usd == 0.0
    assert ledger.budget_state("claude_cli").spent_usd == 0.0


async def test_disabled_ledger_returns_zero_event_without_side_effects(tmp_path: Path) -> None:
    models_yaml, log_path = _basic_yaml(tmp_path, enabled=False)
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    event = ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    assert event.cost_usd == 0.0
    # No JSONL written while disabled
    assert not log_path.exists()
    # check_preflight never raises when disabled
    ledger.check_preflight("chat_brain")


async def test_check_preflight_raises_when_role_subcap_hit(tmp_path: Path) -> None:
    models_yaml, log_path = _basic_yaml(
        tmp_path, per_role={"chat_brain": 0.50, "observer_agent": 0.25}
    )
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    # Spend exactly the observer_agent sub-cap of $0.25.
    # 200k output @ $1.25 / 1M = $0.25
    ledger.record(
        "observer_agent",
        "gpt-5.4-nano",
        CostUsage(input_tokens=0, output_tokens=200_000, cached_tokens=0),
    )

    with pytest.raises(BudgetExhausted) as excinfo:
        ledger.check_preflight("observer_agent")
    assert excinfo.value.scope == "role"
    assert excinfo.value.role == "observer_agent"

    # chat_brain still usable — separate sub-cap path.
    ledger.check_preflight("chat_brain")
