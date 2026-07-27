"""JSONL format: each appended line parses and carries the required fields."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tesseract.brain.cost import CostLedger, CostUsage


_REQUIRED_FIELDS = {
    "ts",
    "local_date",
    "role",
    "model",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cost_usd",
    "daily_total_usd",
    "role_total_usd",
}


def _models_yaml(tmp_path: Path) -> Path:
    data = {
        "providers": {"openai": {"timeout_seconds": 60, "max_retries": 3}},
        "roles": {
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
        },
        "cost_tracking": {
            "enabled": True,
            "warning_at_pct": 0.75,
            "log_file": "logs/cost-tracking.jsonl",
            "per_role": {"chat_brain": 3.00, "observer_agent": 1.00},
        },
    }
    target = tmp_path / "models.yaml"
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


async def test_each_line_is_valid_json_with_required_fields(tmp_path: Path) -> None:
    models_yaml = _models_yaml(tmp_path)
    log_path = tmp_path / "cost.jsonl"
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    ledger.record("chat_brain", "gpt-5.4-nano", CostUsage(100_000, 50_000, 0))
    ledger.record("observer_agent", "gpt-5.4-nano", CostUsage(20_000, 5_000, 10_000))
    ledger.record("chat_brain", "gpt-5.4-nano", CostUsage(200_000, 100_000, 50_000))

    raw_lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(raw_lines) == 3, "expected one JSONL line per record() call"

    for line in raw_lines:
        entry = json.loads(line)  # must parse
        missing = _REQUIRED_FIELDS - set(entry.keys())
        assert not missing, f"entry missing fields {missing}: {entry}"
        assert entry["ts"].endswith("Z"), "timestamps should be UTC (Z suffix)"


async def test_daily_total_is_monotonic_non_decreasing(tmp_path: Path) -> None:
    models_yaml = _models_yaml(tmp_path)
    log_path = tmp_path / "cost.jsonl"
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    for _ in range(5):
        ledger.record("chat_brain", "gpt-5.4-nano", CostUsage(50_000, 10_000, 0))

    entries = [
        json.loads(ln)
        for ln in log_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    totals = [float(e["daily_total_usd"]) for e in entries]
    assert totals == sorted(totals), "daily_total_usd must be monotonic across writes"
    # And strictly increasing when cost > 0
    assert all(totals[i] < totals[i + 1] for i in range(len(totals) - 1))


async def test_role_total_tracks_per_role_spend(tmp_path: Path) -> None:
    models_yaml = _models_yaml(tmp_path)
    log_path = tmp_path / "cost.jsonl"
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    ledger.record("chat_brain", "gpt-5.4-nano", CostUsage(0, 100_000, 0))    # $0.125
    ledger.record("observer_agent", "gpt-5.4-nano", CostUsage(0, 100_000, 0))  # $0.125
    ledger.record("chat_brain", "gpt-5.4-nano", CostUsage(0, 100_000, 0))    # $0.125

    entries = [
        json.loads(ln)
        for ln in log_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    chat_entries = [e for e in entries if e["role"] == "chat_brain"]
    observer_entries = [e for e in entries if e["role"] == "observer_agent"]

    # Each role's own role_total_usd should rise only on its own entries.
    assert chat_entries[-1]["role_total_usd"] == pytest.approx(0.25, abs=1e-9)
    assert observer_entries[-1]["role_total_usd"] == pytest.approx(0.125, abs=1e-9)
    # Global daily total covers both.
    assert entries[-1]["daily_total_usd"] == pytest.approx(0.375, abs=1e-9)
