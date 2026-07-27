"""AU-19 Session 1 — schedule_list / schedule_update / schedule_run.

Each tool routes through ``ToolContext.scheduler_provider`` and
delegates to the existing ``SchedulerEngine`` setters that were
introduced in Phase 18 Task B. The tools own input validation and
error-to-ToolResult shaping; the engine owns the persistence.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.schedule_list import ScheduleListInput, ScheduleListTool
from tesseract.kernel.tools.schedule_run import ScheduleRunInput, ScheduleRunTool
from tesseract.kernel.tools.schedule_update import ScheduleUpdateInput, ScheduleUpdateTool
from tesseract.scheduler.engine import SchedulerEngine


@pytest.fixture
def fresh_config_dir(tmp_path: Path) -> Path:
    src = Path(__file__).resolve().parents[2] / "config"
    target = tmp_path / "config"
    shutil.copytree(src, target)
    return target


def _engine_with_job(config_dir: Path, name: str = "au19_smoke") -> SchedulerEngine:
    engine = SchedulerEngine(config_dir=config_dir)
    engine.add_job_runtime(
        name=name,
        cadence="2h",
        handler="tesseract.scheduler.tasks.vault_lint.VaultLintJob",
    )
    return engine


def test_class_metadata_pinned() -> None:
    assert ScheduleListTool.default_posture == "auto"
    assert ScheduleListTool.risk_class == "autonomous"
    assert ScheduleUpdateTool.default_posture == "ask"
    assert ScheduleUpdateTool.risk_class == "propose"
    assert ScheduleRunTool.default_posture == "ask"
    assert ScheduleRunTool.risk_class == "propose"


async def test_schedule_list_returns_registered_jobs(fresh_config_dir) -> None:
    engine = _engine_with_job(fresh_config_dir)
    tool = ScheduleListTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    result = await tool.run(ScheduleListInput(), ctx)
    assert not result.is_error
    names = {row["name"] for row in result.metadata["jobs"]}
    assert "au19_smoke" in names


async def test_schedule_list_enabled_only_filter(fresh_config_dir) -> None:
    engine = _engine_with_job(fresh_config_dir, name="enabled_job")
    engine.add_job_runtime(
        name="disabled_job",
        cadence="3h",
        handler="tesseract.scheduler.tasks.vault_lint.VaultLintJob",
        enabled=False,
    )
    tool = ScheduleListTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    result = await tool.run(ScheduleListInput(enabled_only=True), ctx)
    names = {row["name"] for row in result.metadata["jobs"]}
    assert "enabled_job" in names
    assert "disabled_job" not in names


async def test_schedule_list_missing_provider() -> None:
    tool = ScheduleListTool()
    ctx = ToolContext(scheduler_provider=None)
    result = await tool.run(ScheduleListInput(), ctx)
    assert result.is_error
    assert "scheduler unavailable" in result.output


async def test_schedule_update_changes_cadence_and_enabled(fresh_config_dir) -> None:
    engine = _engine_with_job(fresh_config_dir)
    tool = ScheduleUpdateTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    result = await tool.run(
        ScheduleUpdateInput(name="au19_smoke", cadence="6h", enabled=False), ctx
    )
    assert not result.is_error
    state = engine.runtime_state("au19_smoke")
    assert state["cadence"] == "6h"
    assert state["enabled"] is False
    assert result.metadata["applied"] == {"cadence": "6h", "enabled": False}


async def test_schedule_update_unknown_job(fresh_config_dir) -> None:
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    tool = ScheduleUpdateTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    result = await tool.run(
        ScheduleUpdateInput(name="ghost", enabled=True), ctx
    )
    assert result.is_error
    assert "not registered" in result.output


def test_schedule_update_input_requires_at_least_one_field() -> None:
    with pytest.raises(ValueError, match="at least one of"):
        ScheduleUpdateInput(name="x")


async def test_schedule_update_invalid_cadence_reports_error(fresh_config_dir) -> None:
    engine = _engine_with_job(fresh_config_dir)
    tool = ScheduleUpdateTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    result = await tool.run(
        ScheduleUpdateInput(name="au19_smoke", cadence="not-a-cadence"), ctx
    )
    assert result.is_error
    assert "failed" in result.output


async def test_schedule_run_fires_job_and_returns_metadata(fresh_config_dir) -> None:
    engine = _engine_with_job(fresh_config_dir)
    # Stub the run path: avoid invoking the real VaultLintJob (touches
    # vault disk). We replace run_now with a deterministic awaitable.
    from tesseract.scheduler.types import JobResult

    async def fake_run_now(name: str) -> JobResult:
        return JobResult(
            job_name=name, run_id="run-test", ok=True, detail="fired", duration_ms=12.5
        )

    engine.run_now = fake_run_now  # type: ignore[assignment]
    tool = ScheduleRunTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    result = await tool.run(ScheduleRunInput(name="au19_smoke"), ctx)
    assert not result.is_error
    assert result.metadata["ok"] is True
    assert result.metadata["run_id"] == "run-test"


async def test_schedule_run_unknown_job(fresh_config_dir) -> None:
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    tool = ScheduleRunTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    result = await tool.run(ScheduleRunInput(name="never_existed"), ctx)
    assert result.is_error
    assert "not registered" in result.output


async def test_schedule_run_propagates_failure_through_is_error(fresh_config_dir) -> None:
    engine = _engine_with_job(fresh_config_dir)
    from tesseract.scheduler.types import JobResult

    async def fake_run_now(name: str) -> JobResult:
        return JobResult(
            job_name=name, run_id="r2", ok=False, detail="boom", duration_ms=4.0
        )

    engine.run_now = fake_run_now  # type: ignore[assignment]
    tool = ScheduleRunTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    result = await tool.run(ScheduleRunInput(name="au19_smoke"), ctx)
    assert result.is_error
    assert result.metadata["ok"] is False
    assert "boom" in result.output
