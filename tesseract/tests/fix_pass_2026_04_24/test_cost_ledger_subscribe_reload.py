"""Phase 3: subscribe() callback fan-out + reload() hot-rehydrate.

subscribe() is the hook Mirror uses to broadcast `cost_delta` envelopes.
reload() is the hook Phase 14 (Settings panel) will call after persisting
yaml edits to models.yaml — caps/warning/pricing must update in place
while daily totals and the JSONL file are preserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tesseract.brain.cost import BudgetExhausted, CostLedger, CostUsage


def _write_yaml(tmp_path: Path, cost_tracking: dict, roles: dict) -> Path:
    data = {
        "providers": {"openai": {"timeout_seconds": 60, "max_retries": 3}},
        "roles": roles,
        "cost_tracking": cost_tracking,
    }
    target = tmp_path / "models.yaml"
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


def _basic_yaml(tmp_path: Path, per_role: dict | None = None,
                cap: float = 3.0, warning: float = 1.0) -> tuple[Path, Path]:
    log_path = tmp_path / "cost.jsonl"
    # `cap` is the desired global cap. We express it as a single per_role
    # entry for "chat_brain" so `CostLedger.cap_usd` (the derived sum)
    # equals `cap`. `warning` is converted to `warning_at_pct` via cap.
    warn_pct = round(warning / cap, 10) if cap > 0 else 0.75
    effective_per_role = per_role if per_role is not None else {"chat_brain": cap}
    models_yaml = _write_yaml(
        tmp_path,
        cost_tracking={
            "enabled": True,
            "warning_at_pct": warn_pct,
            "log_file": "logs/cost-tracking.jsonl",
            "per_role": effective_per_role,
        },
        roles={
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
    )
    return models_yaml, log_path


async def test_subscribe_fires_on_each_record_with_event_and_state(tmp_path: Path) -> None:
    models_yaml, log_path = _basic_yaml(tmp_path, per_role={"chat_brain": 2.0})
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    received = []

    def cb(event, state):
        received.append((event, state))

    ledger.subscribe(cb)

    ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=500_000, output_tokens=500_000),
    )
    ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=200_000, output_tokens=200_000),
    )

    assert len(received) == 2
    e1, s1 = received[0]
    e2, s2 = received[1]
    assert e1.role == "chat_brain"
    assert s1.role_cap_usd == pytest.approx(2.0)
    # Monotonic accumulation.
    assert s2.role_spent_usd > s1.role_spent_usd
    assert s2.spent_usd > s1.spent_usd


async def test_subscriber_exception_does_not_break_record(tmp_path: Path) -> None:
    """A misbehaving subscriber must not raise out of `record()`."""
    models_yaml, log_path = _basic_yaml(tmp_path)
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    calls = []

    def bad_cb(event, state):
        raise RuntimeError("boom")

    def good_cb(event, state):
        calls.append(event.role)

    ledger.subscribe(bad_cb)
    ledger.subscribe(good_cb)

    # Must not raise — and the downstream good_cb still runs.
    ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=1000, output_tokens=1000),
    )
    assert calls == ["chat_brain"]


async def test_subscriber_state_reflects_blocked_after_cap_hit(tmp_path: Path) -> None:
    """When a single `record()` crosses the cap, the state passed to the
    subscriber must already show `blocked=True` so the HUD's sticky toast
    can fire on the same envelope."""
    models_yaml, log_path = _basic_yaml(tmp_path, cap=0.001)
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    captured: list = []
    ledger.subscribe(lambda e, s: captured.append(s))

    ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    assert captured[-1].blocked is True
    with pytest.raises(BudgetExhausted):
        ledger.check_preflight("chat_brain")


async def test_reload_updates_caps_without_losing_daily_totals(tmp_path: Path) -> None:
    models_yaml, log_path = _basic_yaml(tmp_path, cap=3.0, warning=1.0)
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    # Spend $1.45.
    ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    assert ledger.budget_state("chat_brain").spent_usd == pytest.approx(1.45, abs=1e-9)

    # Operator edits the yaml — set per_role so derived cap is 1.0,
    # with warning_at_pct = 0.5 so warning_usd = 0.5.
    raw = yaml.safe_load(models_yaml.read_text())
    raw["cost_tracking"]["warning_at_pct"] = 0.5
    raw["cost_tracking"]["per_role"] = {"chat_brain": 1.0}
    models_yaml.write_text(yaml.safe_dump(raw), encoding="utf-8")

    ledger.reload()

    state = ledger.budget_state("chat_brain")
    assert state.cap_usd == pytest.approx(1.0)
    assert state.warning_usd == pytest.approx(0.5)
    assert state.role_cap_usd == pytest.approx(1.0)
    # Totals preserved — spent still $1.45 even though cap is now $1.
    assert state.spent_usd == pytest.approx(1.45, abs=1e-9)
    assert state.blocked is True  # spend > new cap
    with pytest.raises(BudgetExhausted):
        ledger.check_preflight("chat_brain")


async def test_reload_updates_pricing_for_subsequent_records(tmp_path: Path) -> None:
    models_yaml, log_path = _basic_yaml(tmp_path)
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    # Edit pricing: 10× the output rate.
    raw = yaml.safe_load(models_yaml.read_text())
    raw["roles"]["chat_brain"]["resolution"][0]["cost_per_mtok_out"] = 12.50
    models_yaml.write_text(yaml.safe_dump(raw), encoding="utf-8")
    ledger.reload()

    event = ledger.record(
        "chat_brain",
        "gpt-5.4-nano",
        CostUsage(input_tokens=0, output_tokens=1_000_000),
    )
    # Now 1M output @ $12.50 = $12.50, up from $1.25.
    assert event.cost_usd == pytest.approx(12.50, abs=1e-9)


async def test_reload_preserves_subscribers(tmp_path: Path) -> None:
    models_yaml, log_path = _basic_yaml(tmp_path)
    ledger = CostLedger.from_models_yaml(models_yaml=models_yaml, log_path=log_path)

    hits = []
    ledger.subscribe(lambda e, s: hits.append(e.role))

    ledger.reload()  # Subscribers survive the reload.

    ledger.record("chat_brain", "gpt-5.4-nano",
                  CostUsage(input_tokens=100, output_tokens=100))
    assert hits == ["chat_brain"]
