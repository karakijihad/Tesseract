"""Phase 1 / F6 — global daily USD ceiling for autonomous dispatch.

Verifies the USD branch wired into ``AutonomyKernel._check_daily_caps``:
the cap gates on an injected ``daily_usd_spent`` accessor (the cost
ledger's global daily total), pauses dispatch at/over the cap, is skipped
when no accessor is wired, and fails open if the accessor raises.

All tests run under a monkeypatched ``TESSERACT_HOME`` so the AgendaStore
writes into ``tmp_path`` — production state (incl. ``tesseract/logs/**``)
stays untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy import (
    AgendaStore,
    AutonomyEvent,
    AutonomyEventBus,
    AutonomyKernel,
    KernelConfig,
    MapperConfig,
)
from tesseract.orchestrator.autonomy.kernel import REASON_DAILY_CAP_PAUSE
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass
from tesseract.orchestrator.autonomy.publishers import set_active_bus
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import WorkerLane


def _make_kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    usd_cap: float,
    spent: float | None,
    raises: bool = False,
) -> AutonomyKernel:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    if raises:
        def _accessor() -> float:
            raise RuntimeError("ledger snapshot unavailable")
    elif spent is None:
        _accessor = None  # type: ignore[assignment]
    else:
        def _accessor() -> float:
            return spent

    return AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=WorkerLane({WorkerKind.TARS_SELF: 10}),
        config=KernelConfig(top_k=3, daily_tokens_cap=0, daily_seconds_cap=0, daily_usd_cap=usd_cap),
        daily_usd_spent=_accessor,
    )


def test_usd_cap_loads_from_yaml() -> None:
    cfg = KernelConfig.from_yaml_dict({"daily_caps": {"usd": 5.0, "tokens": 100, "seconds": 60}})
    assert cfg.daily_usd_cap == 5.0
    assert cfg.daily_tokens_cap == 100
    assert cfg.daily_seconds_cap == 60


def test_usd_cap_absent_defaults_zero() -> None:
    cfg = KernelConfig.from_yaml_dict({"daily_caps": {"tokens": 100}})
    assert cfg.daily_usd_cap == 0.0


def test_usd_cap_pauses_at_or_over_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    k = _make_kernel(tmp_path, monkeypatch, usd_cap=5.0, spent=5.0)
    assert k._check_daily_caps() == REASON_DAILY_CAP_PAUSE
    set_active_bus(None)


def test_usd_cap_over_budget_pauses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    k = _make_kernel(tmp_path, monkeypatch, usd_cap=5.0, spent=7.31)
    assert k._check_daily_caps() == REASON_DAILY_CAP_PAUSE
    set_active_bus(None)


def test_usd_cap_under_budget_allows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    k = _make_kernel(tmp_path, monkeypatch, usd_cap=5.0, spent=4.99)
    assert k._check_daily_caps() is None
    set_active_bus(None)


def test_usd_cap_skipped_without_accessor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cap set, but no ledger wired (test/boot-fail) → never silently faked.
    k = _make_kernel(tmp_path, monkeypatch, usd_cap=5.0, spent=None)
    assert k._check_daily_caps() is None
    set_active_bus(None)


def test_usd_accessor_failure_is_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A broken accessor must not wedge the kernel — treat as under-cap.
    k = _make_kernel(tmp_path, monkeypatch, usd_cap=5.0, spent=None, raises=True)
    assert k._check_daily_caps() is None
    set_active_bus(None)


def test_usd_cap_zero_disables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # cap 0 → disabled even with an accessor that would exceed any positive cap.
    k = _make_kernel(tmp_path, monkeypatch, usd_cap=0.0, spent=1000.0)
    assert k._check_daily_caps() is None
    set_active_bus(None)


@pytest.mark.asyncio
async def test_usd_cap_pauses_full_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: a tick pauses selection when global spend is at the cap,
    mirroring the existing token-cap tick test."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    mappers = {
        source: MapperConfig(
            enabled=True, source=source, default_risk_class=RiskClass.PROPOSE, dedupe_window_hours=24
        )
        for source in (AgendaSource.OPERATOR,)
    }
    k = AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=WorkerLane({WorkerKind.TARS_SELF: 10}),
        config=KernelConfig(top_k=3, daily_usd_cap=5.0),
        mapper_configs=mappers,
        daily_usd_spent=lambda: 5.0,
        event_bus=AutonomyEventBus(),
    )
    k.bus.publish_nowait(AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-usd-cap"}))
    result = await k.tick()
    assert result.paused is True
    assert k.dispatch_paused is True
    assert k.dispatch_pause_reason == REASON_DAILY_CAP_PAUSE
    set_active_bus(None)
