"""M3 regression — `SchedulerEngine.stop()` must cancel / await in-flight jobs.

Pre-fix, `stop()` cancelled only `_task` and `_alarm_task`; jobs spawned
via `asyncio.create_task(self._run_job(...))` from catch-up, `_tick`, or
Mirror's `/schedule-run-now` were orphaned and kept running after
shutdown returned.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.engine import SchedulerEngine
from tesseract.scheduler.types import JobContext, JobResult


class _SleepyJob(BaseJob):
    """Job that sleeps longer than the test is willing to wait, to expose
    whether `stop()` actually cancels in-flight tasks."""

    async def run(self, ctx: JobContext) -> JobResult:
        try:
            await asyncio.sleep(30)
            return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=True, detail="slept")
        except asyncio.CancelledError:
            return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=False, detail="cancelled")


def _write_schedule(config_dir: Path, handler: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "schedule.yaml").write_text(
        yaml.safe_dump({
            "catchup": {"concurrency": 8},
            "jobs": [
                {
                    "name": "sleepy",
                    "cadence": "1h",
                    "enabled": True,
                    "handler": handler,
                    "config": {},
                    "on_failure": "log",
                    "retry_policy": {"max_retries": 0, "backoff_seconds": 0},
                }
            ]
        }),
        encoding="utf-8",
    )


async def test_stop_cancels_manually_triggered_run_now(tmp_path, monkeypatch) -> None:
    """A `run_now` started via `spawn_tracked_task` is cancelled by `stop()`."""
    log_dir = tmp_path / "logs"
    config_dir = tmp_path / "config"
    _write_schedule(config_dir, handler=f"{__name__}._SleepyJob")

    engine = SchedulerEngine(config_dir=config_dir, log_dir=log_dir, stop_join_timeout=0.2)
    await engine.start(app=None)

    task = engine.spawn_tracked_task(
        engine.run_now("sleepy"),
        name="scheduler-run-now-sleepy",
    )
    await asyncio.sleep(0.05)  # let the job begin
    assert not task.done()
    assert task in engine._inflight

    await engine.stop()

    assert task.done(), "stop() must finish in-flight job tasks"
    assert engine._inflight == set()
