"""Verify the shared, observer, and scheduler circuit breakers all consult
the same YAML key (`availability.max_consecutive_failures` in `providers.yaml`).

Prior to 2026-04-23 each module hardcoded `= 3` independently, so a config
change would silently not propagate. This test pins the invariant: one YAML
edit must move all three.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _yaml_max_failures() -> int:
    cfg_path = Path(__file__).resolve().parents[2] / "config" / "providers.yaml"
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return int(cfg["availability"]["max_consecutive_failures"])


def test_shared_breaker_reads_yaml() -> None:
    from tesseract.context.circuit_breaker import (
        CircuitBreaker,
        _default_max_failures,
    )

    _default_max_failures.cache_clear()
    expected = _yaml_max_failures()
    assert _default_max_failures() == expected

    cb = CircuitBreaker(name="unit_yaml_probe")
    assert cb.max_failures == expected


def test_observer_breaker_reads_yaml() -> None:
    from tesseract.brain.observer_budget import CircuitBreaker as ObserverBreaker
    from tesseract.context.circuit_breaker import _default_max_failures

    _default_max_failures.cache_clear()
    expected = _yaml_max_failures()

    cb = ObserverBreaker()
    assert cb._max == expected


def test_scheduler_module_constant_reads_yaml() -> None:
    from tesseract.context.circuit_breaker import _default_max_failures
    from tesseract.scheduler import engine as scheduler_engine

    _default_max_failures.cache_clear()
    expected = _yaml_max_failures()
    assert scheduler_engine.MAX_CONSECUTIVE_FAILURES == expected
