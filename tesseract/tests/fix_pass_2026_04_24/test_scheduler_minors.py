"""audit-1 Minor regressions: m1 placeholder-disabled, m5 cron double-fire,
m10 broadcast import cache.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tesseract.scheduler.engine import (
    SchedulerEngine,
    _PlaceholderJob,
    _broadcast_envelope,
)


def _write(tmp_path: Path, job: dict) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "schedule.yaml").write_text(
        yaml.safe_dump({"catchup": {"concurrency": 8}, "jobs": [job]}), encoding="utf-8"
    )
    return cfg


def _job(**overrides) -> dict:
    base = {
        "name": "ticker",
        "cadence": "0 12 * * *",
        "handler": "totally.missing.Handler",
        "enabled": True,
        "on_failure": "log",
        "retry_policy": {"max_retries": 0, "backoff_seconds": 0},
    }
    base.update(overrides)
    return base


# ── m1 ────────────────────────────────────────────────────────────────────


def test_placeholder_handler_starts_disabled(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _job())
    engine = SchedulerEngine(config_dir=cfg, log_dir=tmp_path / "logs")
    rt = engine.registry["ticker"]
    assert rt.handler_cls is _PlaceholderJob
    assert rt.enabled is False


# ── m5 ────────────────────────────────────────────────────────────────────


def test_should_fire_blocks_in_slot_repeat(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _job(cadence="*/1 * * * *"))
    engine = SchedulerEngine(config_dir=cfg, log_dir=tmp_path / "logs")
    rt = engine.registry["ticker"]
    rt.enabled = True

    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
    # `_should_fire` now takes both UTC (storage/dedupe) and local-naive
    # (cron matching) — `*/1 * * * *` matches every minute regardless of tz,
    # so we can pass the same value for both in this test.
    now_local_naive = now.astimezone().replace(tzinfo=None)
    # First fire — no history, `_should_fire` returns True.
    assert engine._should_fire(rt, now, now_local_naive) is True
    rt.last_fired_at = now
    # Same minute — must not fire again.
    later_15 = now + timedelta(seconds=15)
    assert engine._should_fire(rt, later_15, later_15.astimezone().replace(tzinfo=None)) is False
    # One minute later — cron matches AND 60s guard satisfied.
    later_60 = now + timedelta(seconds=60)
    assert engine._should_fire(rt, later_60, later_60.astimezone().replace(tzinfo=None)) is True


# ── m10 ───────────────────────────────────────────────────────────────────


async def test_broadcast_envelope_cache_suppresses_repeated_import_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the Mirror import fails, subsequent calls short-circuit silently
    rather than re-raising or re-logging."""
    from tesseract.scheduler import engine as engine_module

    monkeypatch.setattr(engine_module, "_MIRROR_BROADCAST", None)
    monkeypatch.setattr(engine_module, "_MIRROR_BROADCAST_FAILED", True)

    class _App:
        def __init__(self) -> None:
            self.store: dict = {
                "server_sessions": {"a": object()},
            }

        def get(self, key, default=None):
            return self.store.get(key, default)

    # Must not raise even though the helpers are flagged as unavailable.
    await _broadcast_envelope(_App(), "schedule_job_done", {"x": 1})
    await _broadcast_envelope(_App(), "schedule_job_done", {"x": 2})
