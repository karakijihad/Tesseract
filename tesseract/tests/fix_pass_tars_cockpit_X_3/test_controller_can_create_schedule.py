"""X-3 — ControllerRuntime wires a real ``SchedulerEngine`` so
``schedule_create`` succeeds from inside a controller chat.

Pre-X-3 ``tars_controller.py`` built the ``ToolContext`` with
``scheduler_provider=lambda: None`` — every ``schedule_*`` tool short-
circuited with "scheduler unavailable". X-3 replaces the stub with a
bound method that returns the runtime's live engine.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.schedule_create import (
    ScheduleCreateInput,
    ScheduleCreateTool,
)
from tesseract.scheduler.engine import SchedulerEngine
from tesseract.scripts.tars_controller import ControllerRuntime


def test_rebuild_scheduler_produces_real_engine(
    isolated_home: Path, isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_rebuild_scheduler`` populates ``runtime.scheduler`` with a live
    ``SchedulerEngine`` instance — no ``None`` left over."""
    from tesseract import paths as paths_module

    monkeypatch.setattr(paths_module, "CONFIG_DIR", isolated_config_dir)
    runtime = ControllerRuntime()
    reloaded, failed = runtime._rebuild_scheduler()

    assert "scheduler" in reloaded, failed
    assert failed == []
    assert isinstance(runtime.scheduler, SchedulerEngine)
    assert runtime._get_scheduler() is runtime.scheduler


def test_schedule_create_succeeds_via_runtime_provider(
    isolated_home: Path, isolated_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Done-criterion 4: ``schedule_create`` succeeds + job appears in
    the scheduler instance the runtime exposes via the provider."""
    from tesseract import paths as paths_module

    monkeypatch.setattr(paths_module, "CONFIG_DIR", isolated_config_dir)
    runtime = ControllerRuntime()
    runtime._rebuild_scheduler()

    tool = ScheduleCreateTool()
    ctx = ToolContext(
        workspace_root=str(isolated_home),
        session_id="x3-test",
        scheduler_provider=runtime._get_scheduler,
    )
    inp = ScheduleCreateInput(
        name="x3_test_job",
        cadence="15m",
        handler="tesseract.scheduler.tasks.alarm_handler.AlarmHandlerJob",
        enabled=False,  # don't arm a live fire path
    )

    result = asyncio.run(tool.run(inp, ctx))

    assert not result.is_error, result.output
    assert "x3_test_job" in runtime.scheduler.registry
    # Persisted to disk: schedule.yaml now lists the new job.
    yaml_text = (isolated_config_dir / "schedule.yaml").read_text(encoding="utf-8")
    assert "x3_test_job" in yaml_text


def test_get_scheduler_returns_none_before_rebuild() -> None:
    """Honest contract: before ``_rebuild_scheduler`` runs the provider
    returns ``None`` so dependent tools degrade with a clean error
    instead of crashing inside the brain loop."""
    runtime = ControllerRuntime()

    assert runtime.scheduler is None
    assert runtime._get_scheduler() is None
