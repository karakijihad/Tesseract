"""CostLedger operator controls added for the MCP budget.* verbs (P3 s3):
set_role_cap, pause_source/resume_source, budget_summary, and the paused-source
guard in check_preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.brain.cost.ledger import BudgetExhausted, CostLedger


def _ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(
        enabled=True,
        warning_at_pct=0.75,
        per_role_caps={"chat_brain": 10.0},
        pricing={},
        log_path=tmp_path / "cost.jsonl",
    )


def test_set_role_cap_overrides_runtime(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.set_role_cap("chat_brain", 2.5)
    assert ledger.per_role_caps["chat_brain"] == 2.5
    ledger.set_role_cap("observer_agent", 1.0)  # new role
    assert ledger.per_role_caps["observer_agent"] == 1.0


def test_set_role_cap_rejects_negative(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(ValueError):
        ledger.set_role_cap("chat_brain", -1.0)


def test_pause_source_blocks_preflight(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    # Not paused, well under cap → no raise.
    ledger.check_preflight("chat_brain")
    ledger.pause_source("chat_brain")
    with pytest.raises(BudgetExhausted) as exc:
        ledger.check_preflight("chat_brain")
    assert exc.value.scope == "paused"
    # Resume lifts it.
    ledger.resume_source("chat_brain")
    ledger.check_preflight("chat_brain")


def test_pause_global_blocks_every_role(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.pause_source("global")
    with pytest.raises(BudgetExhausted):
        ledger.check_preflight("chat_brain")


def test_budget_summary_shape(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.pause_source("global")
    summary = ledger.budget_summary()
    assert summary["enabled"] is True
    assert summary["cap_usd"] == 10.0
    assert summary["per_role_caps"] == {"chat_brain": 10.0}
    assert summary["paused_sources"] == ["global"]
