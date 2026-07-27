"""Cache-aware pricing — explicit cached rate + Anthropic cache-creation surcharge.

Two real gaps closed by this fix-pass:

1. Anthropic `cache_creation_input_tokens` were captured by the adapter and
   then silently dropped when the ledger built `CostUsage`. The surcharge
   (1.25× base input rate per Anthropic's published prompt-cache docs)
   never reached the daily total.
2. Cached input was hardcoded to 10% of the base rate. That's correct for
   OpenAI and for Anthropic cache-reads, but wrong for Gemini (~25%). The
   ledger now honours an explicit `cost_per_mtok_cached_in` per model and
   falls back to 10% only when absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tesseract.brain.cost import CostLedger, CostUsage
from tesseract.brain.cost.ledger import CACHE_CREATION_RATE, CACHED_INPUT_RATE


def _write_yaml(tmp_path: Path, roles: dict) -> Path:
    data = {
        "providers": {"openai": {"timeout_seconds": 60, "max_retries": 3}},
        "roles": roles,
        "cost_tracking": {
            "enabled": True,
            "warning_at_pct": 0.75,
            "log_file": "logs/cost-tracking.jsonl",
        },
    }
    target = tmp_path / "models.yaml"
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


async def test_cache_creation_tokens_billed_at_125_percent(tmp_path: Path) -> None:
    """Anthropic cache-write surcharge: 1.25× base input rate per token."""
    models_yaml = _write_yaml(
        tmp_path,
        roles={
            "chat_brain": {
                "resolution": [
                    {
                        "model": "claude-opus-4-7",
                        "cost_per_mtok_in": 15.00,
                        "cost_per_mtok_out": 75.00,
                    }
                ]
            },
        },
    )
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=tmp_path / "cost.jsonl")

    # 1k uncached input + 500 cache-creation tokens, no output, no cache reads.
    event = ledger.record(
        "chat_brain",
        "claude-opus-4-7",
        CostUsage(input_tokens=1000, output_tokens=0, cached_tokens=0, cache_creation_tokens=500),
    )

    assert CACHE_CREATION_RATE == pytest.approx(1.25)
    expected = (1000 * 15.00 + 500 * 15.00 * 1.25) / 1_000_000
    assert event.cost_usd == pytest.approx(expected, abs=1e-9)


async def test_cache_creation_zero_when_field_absent(tmp_path: Path) -> None:
    """Adapters that don't populate the field leave it at 0 — surcharge is no-op."""
    models_yaml = _write_yaml(
        tmp_path,
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
        },
    )
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=tmp_path / "cost.jsonl")

    event = ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=0),
    )
    # Sanity: identical to pre-fix math when the new field is unset.
    assert event.cost_usd == pytest.approx(1.45, abs=1e-9)


async def test_explicit_cached_rate_overrides_ten_percent_default(tmp_path: Path) -> None:
    """When `cost_per_mtok_cached_in` is set, the ledger uses it directly."""
    models_yaml = _write_yaml(
        tmp_path,
        roles={
            "chat_brain": {
                "resolution": [
                    {
                        "model": "gemini-2.5-flash",
                        "cost_per_mtok_in": 0.30,
                        "cost_per_mtok_out": 2.50,
                        # Gemini cached input is ~25% of base, not 10%.
                        "cost_per_mtok_cached_in": 0.075,
                    }
                ]
            },
        },
    )
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=tmp_path / "cost.jsonl")

    # 1M cached input, no output. Default 10% would give $0.030; explicit
    # 25% gives $0.075. Verifying the higher number proves we honoured the
    # explicit rate and didn't fall back to the OpenAI default.
    event = ledger.record(
        "chat_brain",
        "gemini-2.5-flash",
        CostUsage(input_tokens=1_000_000, output_tokens=0, cached_tokens=1_000_000),
    )
    assert event.cost_usd == pytest.approx(0.075, abs=1e-9)


async def test_default_cached_rate_still_ten_percent(tmp_path: Path) -> None:
    """Models without an explicit cached rate keep the 10% fallback."""
    models_yaml = _write_yaml(
        tmp_path,
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
        },
    )
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=tmp_path / "cost.jsonl")

    event = ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=1_000_000, output_tokens=0, cached_tokens=800_000),
    )
    expected = (200_000 * 0.20 + 800_000 * 0.20 * CACHED_INPUT_RATE) / 1_000_000
    assert event.cost_usd == pytest.approx(expected, abs=1e-9)


async def test_combined_cache_read_and_creation_anthropic(tmp_path: Path) -> None:
    """Realistic Anthropic turn with all four token classes contributing."""
    models_yaml = _write_yaml(
        tmp_path,
        roles={
            "chat_brain": {
                "resolution": [
                    {
                        "model": "claude-sonnet-4-6",
                        "cost_per_mtok_in": 3.00,
                        "cost_per_mtok_out": 15.00,
                    }
                ]
            },
        },
    )
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=tmp_path / "cost.jsonl")

    # 1000 input total (200 uncached after subtracting 800 cache reads),
    # 200 cache-creation surcharge, 50 output. Mirrors the ledger's existing
    # `uncached = input - cached` arithmetic, which is the OpenAI shape.
    event = ledger.record(
        "chat_brain",
        "claude-sonnet-4-6",
        CostUsage(
            input_tokens=1000,
            output_tokens=50,
            cached_tokens=800,
            cache_creation_tokens=200,
        ),
    )
    expected = (
        200 * 3.00              # uncached input
        + 800 * 3.00 * 0.10     # cache reads at 10%
        + 200 * 3.00 * 1.25     # cache writes at 125%
        + 50 * 15.00            # output
    ) / 1_000_000
    assert event.cost_usd == pytest.approx(expected, abs=1e-9)
