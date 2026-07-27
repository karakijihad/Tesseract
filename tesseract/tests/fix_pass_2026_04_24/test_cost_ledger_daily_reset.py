"""Local-tz midnight rollover + boot re-seeding from today's JSONL."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tesseract.brain.cost import CostLedger, CostUsage


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
            }
        },
        "cost_tracking": {
            "enabled": True,
            "warning_at_pct": 0.75,
            "log_file": "logs/cost-tracking.jsonl",
            "per_role": {"chat_brain": 3.00},
        },
    }
    target = tmp_path / "models.yaml"
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


def _write_entries(log_path: Path, entries: list[dict]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


async def test_seed_from_log_only_reads_today(tmp_path: Path) -> None:
    models_yaml = _models_yaml(tmp_path)
    log_path = tmp_path / "cost.jsonl"
    _write_entries(
        log_path,
        [
            {"local_date": "2026-04-23", "role": "chat_brain", "cost_usd": 0.50},
            {"local_date": "2026-04-24", "role": "chat_brain", "cost_usd": 0.10},
            {"local_date": "2026-04-24", "role": "observer_agent", "cost_usd": 0.05},
        ],
    )

    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml,
        log_path=log_path,
        today_fn=lambda: "2026-04-24",
    )

    state = ledger.budget_state("chat_brain")
    # Yesterday's $0.50 is ignored; today totals = $0.10 + $0.05 = $0.15
    assert state.spent_usd == pytest.approx(0.15, abs=1e-9)
    assert state.role_spent_usd == pytest.approx(0.10, abs=1e-9)
    assert ledger.budget_state("observer_agent").role_spent_usd == pytest.approx(0.05, abs=1e-9)


async def test_midnight_rollover_resets_totals(tmp_path: Path) -> None:
    models_yaml = _models_yaml(tmp_path)
    log_path = tmp_path / "cost.jsonl"

    fake_today = {"value": "2026-04-24"}
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml,
        log_path=log_path,
        today_fn=lambda: fake_today["value"],
    )

    # Spend $0.10 on 04-24 — 1M input @ $0.20 / 1M = $0.20... let's pick 500k input → $0.10
    ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=500_000, output_tokens=0),
    )
    assert ledger.budget_state("chat_brain").spent_usd == pytest.approx(0.10, abs=1e-9)

    # Midnight passes.
    fake_today["value"] = "2026-04-25"
    # budget_state() triggers the rollover check.
    state = ledger.budget_state("chat_brain")
    assert state.spent_usd == 0.0
    assert state.role_spent_usd == 0.0

    # A new turn after midnight lands on the new day.
    ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=500_000, output_tokens=0),
    )
    state = ledger.budget_state("chat_brain")
    assert state.spent_usd == pytest.approx(0.10, abs=1e-9)


async def test_seed_ignores_corrupted_jsonl_lines(tmp_path: Path) -> None:
    models_yaml = _models_yaml(tmp_path)
    log_path = tmp_path / "cost.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Mix of valid + garbage lines.
    log_path.write_text(
        '\n'.join(
            [
                '{"local_date": "2026-04-24", "role": "chat_brain", "cost_usd": 0.05}',
                'not-json-at-all',
                '',
                '{"local_date": "2026-04-24", "role": "chat_brain", "cost_usd": 0.10}',
            ]
        )
        + '\n',
        encoding="utf-8",
    )

    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml,
        log_path=log_path,
        today_fn=lambda: "2026-04-24",
    )
    assert ledger.budget_state("chat_brain").spent_usd == pytest.approx(0.15, abs=1e-9)


async def test_missing_log_file_seeds_to_zero(tmp_path: Path) -> None:
    models_yaml = _models_yaml(tmp_path)
    log_path = tmp_path / "does-not-exist.jsonl"
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml,
        log_path=log_path,
        today_fn=lambda: "2026-04-24",
    )
    assert ledger.budget_state("chat_brain").spent_usd == 0.0
    assert not log_path.exists()
