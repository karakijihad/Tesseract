"""Phase 18 Task B — runtime schedule mutation.

Covers:
- `add_job_runtime` writes schedule.yaml + arms registry
- `remove_job_runtime` removes from yaml + registry
- Handler whitelist refuses arbitrary import paths
- Cadence validation refuses garbage
- Idempotency on name collision
- `schedule_create`/`schedule_remove` tools route through scheduler_provider
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.schedule_create import ScheduleCreateInput, ScheduleCreateTool
from tesseract.kernel.tools.schedule_remove import ScheduleRemoveInput, ScheduleRemoveTool
from tesseract.scheduler.config_loader import RetryPolicy, load_schedule_config
from tesseract.scheduler.engine import SchedulerEngine


@pytest.fixture
def fresh_config_dir(tmp_path: Path) -> Path:
    src = Path(__file__).resolve().parents[2] / "config"
    target = tmp_path / "config"
    shutil.copytree(src, target)
    return target


def test_add_job_runtime_persists_and_arms(fresh_config_dir):
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    initial = set(engine.registry)
    cfg = engine.add_job_runtime(
        name="phase18_smoke",
        cadence="2h",
        handler="tesseract.scheduler.tasks.vault_lint.VaultLintJob",
        enabled=True,
        on_failure="log",
    )
    assert cfg.name == "phase18_smoke"
    assert "phase18_smoke" in engine.registry
    # Persisted to disk
    on_disk = load_schedule_config(fresh_config_dir)
    names = {j.name for j in on_disk.jobs}
    assert "phase18_smoke" in names
    assert names == initial | {"phase18_smoke"}


def test_remove_job_runtime_strips_yaml_and_registry(fresh_config_dir):
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    engine.add_job_runtime(
        name="to_remove",
        cadence="6h",
        handler="tesseract.scheduler.tasks.vault_lint.VaultLintJob",
    )
    cfg = engine.remove_job_runtime("to_remove")
    assert cfg.name == "to_remove"
    assert "to_remove" not in engine.registry
    on_disk = load_schedule_config(fresh_config_dir)
    assert "to_remove" not in {j.name for j in on_disk.jobs}


def test_remove_unknown_job_raises_keyerror(fresh_config_dir):
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    with pytest.raises(KeyError):
        engine.remove_job_runtime("never_existed")


def test_add_rejects_outside_whitelist(fresh_config_dir):
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    with pytest.raises(ValueError, match="outside the allowed prefixes"):
        engine.add_job_runtime(
            name="evil",
            cadence="1h",
            handler="os.system",  # NOT a tasks.* handler
        )


def test_add_rejects_invalid_cadence(fresh_config_dir):
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    with pytest.raises(ValueError, match="invalid cadence"):
        engine.add_job_runtime(
            name="bad_cron",
            cadence="not-a-cadence",
            handler="tesseract.scheduler.tasks.vault_lint.VaultLintJob",
        )


def test_add_rejects_collision(fresh_config_dir):
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    engine.add_job_runtime(
        name="dup",
        cadence="1h",
        handler="tesseract.scheduler.tasks.vault_lint.VaultLintJob",
    )
    with pytest.raises(ValueError, match="already exists"):
        engine.add_job_runtime(
            name="dup",
            cadence="2h",
            handler="tesseract.scheduler.tasks.vault_lint.VaultLintJob",
        )


def test_add_rejects_bad_handler_import(fresh_config_dir):
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    # Inside the whitelist prefix but the class doesn't exist.
    with pytest.raises(ValueError, match="not importable"):
        engine.add_job_runtime(
            name="ghost",
            cadence="1h",
            handler="tesseract.scheduler.tasks.vault_lint.NonexistentClass",
        )


async def test_schedule_create_tool_uses_provider(fresh_config_dir):
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    tool = ScheduleCreateTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    inp = ScheduleCreateInput(
        name="tool_made",
        cadence="3h",
        handler="tesseract.scheduler.tasks.vault_lint.VaultLintJob",
    )
    result = await tool.run(inp, ctx)
    assert not result.is_error
    assert "tool_made" in engine.registry
    assert result.metadata["name"] == "tool_made"


async def test_schedule_create_tool_handles_missing_provider():
    tool = ScheduleCreateTool()
    ctx = ToolContext(scheduler_provider=None)
    inp = ScheduleCreateInput(
        name="x", cadence="1h",
        handler="tesseract.scheduler.tasks.vault_lint.VaultLintJob",
    )
    result = await tool.run(inp, ctx)
    assert result.is_error
    assert "scheduler unavailable" in result.output


async def test_schedule_remove_tool_round_trip(fresh_config_dir):
    engine = SchedulerEngine(config_dir=fresh_config_dir)
    engine.add_job_runtime(
        name="kill_me",
        cadence="1h",
        handler="tesseract.scheduler.tasks.vault_lint.VaultLintJob",
    )
    tool = ScheduleRemoveTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    result = await tool.run(ScheduleRemoveInput(name="kill_me"), ctx)
    assert not result.is_error
    assert "kill_me" not in engine.registry


async def test_schedule_remove_tool_unknown_job():
    engine = SchedulerEngine(config_dir=Path(__file__).resolve().parents[2] / "config")
    tool = ScheduleRemoveTool()
    ctx = ToolContext(scheduler_provider=lambda: engine)
    result = await tool.run(ScheduleRemoveInput(name="nonsense"), ctx)
    assert result.is_error
    assert "not registered" in result.output
