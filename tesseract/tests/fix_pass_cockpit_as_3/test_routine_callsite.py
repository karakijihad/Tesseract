from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.engine import SchedulerEngine
from tesseract.scheduler.types import JobContext, JobResult
from tesseract.orchestrator.activity.registry import get_activity_registry, reset_activity_registry


def _build_engine(tmp_path: Path) -> SchedulerEngine:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "schedule.yaml").write_text(
        yaml.safe_dump({"catchup": {"concurrency": 8}, "jobs": [{
            "name": "ticker", "cadence": "*/1 * * * *",
            "handler": "tests.does_not_exist.NoOp", "enabled": True,
            "on_failure": "log", "retry_policy": {"max_retries": 0, "backoff_seconds": 0},
        }]}),
        encoding="utf-8",
    )
    engine = SchedulerEngine(config_dir=config_dir, log_dir=tmp_path / "logs")
    for rt in engine.registry.values():
        rt.enabled = True
    return engine


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    reset_activity_registry()
    yield
    reset_activity_registry()


@pytest.mark.asyncio
async def test_run_job_registers_routine_during_run_and_removes_after(tmp_path):
    engine = _build_engine(tmp_path)
    rt = engine.registry["ticker"]
    seen = {}

    class _CapturingJob(BaseJob):
        async def run(self, ctx: JobContext) -> JobResult:
            rec = get_activity_registry().get(f"routine:{ctx.run_id}")
            seen["mid"] = rec
            return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=True)

    rt.handler_cls = _CapturingJob
    await engine._run_job("ticker", rt, datetime.now(timezone.utc))

    assert seen["mid"] is not None
    assert seen["mid"].kind == "routine"
    assert seen["mid"].state == "running"
    assert seen["mid"].label == "ticker"
    assert get_activity_registry().snapshot() == []


@pytest.mark.asyncio
async def test_run_job_failure_transitions_routine_to_failed_not_removed(tmp_path):
    """2026-07-05: a FAILED run must stay visible to the operator — the
    registry record transitions to ``failed`` (carrying the job's short
    error detail) instead of being removed like a successful run."""
    engine = _build_engine(tmp_path)
    rt = engine.registry["ticker"]

    class _FailingJob(BaseJob):
        async def run(self, ctx: JobContext) -> JobResult:
            return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=False, detail="boom: disk full")

    rt.handler_cls = _FailingJob
    result = await engine._run_job("ticker", rt, datetime.now(timezone.utc))

    assert result.ok is False
    rec = get_activity_registry().get(f"routine:{result.run_id}")
    assert rec is not None, "a FAILED routine must remain in the registry, not be removed"
    assert rec.state == "failed"
    assert rec.result == "boom: disk full"
