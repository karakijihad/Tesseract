"""AU-3 S2 — RecoveryManager scan 2 walks durable worker records and
classifies them per kind. Integration test that exercises the manager
end-to-end with on-disk records."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    WorkerStatus,
    write_record,
)
from tesseract.orchestrator.workers.recovery import reset_recovery_handlers
from tesseract.tests.fix_pass_autonomy_AU_3.conftest import make_record


@pytest.fixture
def recovery_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Same pattern as fix_pass_autonomy_AU_2: monkeypatch TESSERACT_HOME
    AND reload the paths + recovery modules so the manager picks up the
    tmp dir as its tesseract_home anchor."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    import tesseract.orchestrator.recovery
    importlib.reload(tesseract.orchestrator.recovery.manager)
    importlib.reload(tesseract.orchestrator.recovery)
    reset_recovery_handlers()
    yield tmp_path
    reset_recovery_handlers()


@pytest.mark.asyncio
async def test_scan_marks_running_workers_interrupted(recovery_home: Path) -> None:
    write_record(make_record(kind=WorkerKind.CLAUDE_CLI, status=WorkerStatus.RUNNING, agenda_item_id="ag-1"))
    write_record(make_record(kind=WorkerKind.TARS_SELF, status=WorkerStatus.RUNNING, agenda_item_id="ag-2"))
    write_record(make_record(kind=WorkerKind.MARKDOWN_AGENT, status=WorkerStatus.DONE, agenda_item_id="ag-3"))

    from tesseract.orchestrator.recovery.manager import new_recovery_manager

    manager = new_recovery_manager(tesseract_home=recovery_home)
    summary = await manager.run(emit_event=False)

    workers_block = summary.scans.get("workers", {})
    # Two non-terminal records → both interrupted; one DONE → preserved.
    assert workers_block.get("interrupted", 0) == 2
    assert workers_block.get("preserved", 0) >= 1
    # Operator attention should list both interrupted workers.
    worker_ids = {a.id for a in summary.operator_attention if a.kind == "worker"}
    assert len(worker_ids) == 2


@pytest.mark.asyncio
async def test_scan_isolates_malformed_records(recovery_home: Path) -> None:
    """A single corrupt record.json must not blow up the scan; the
    scan_error attention item is the per-worker fallback, not a
    full-blown scan failure."""
    write_record(make_record(kind=WorkerKind.TARS_SELF, status=WorkerStatus.RUNNING, agenda_item_id="ag-good"))

    # Inject a malformed neighbor. The cheap scan skips it silently so
    # `_scan_workers` never sees the id — confirm no exception is raised
    # and the good record still lands as interrupted.
    bad_dir = recovery_home / "workers" / "active" / "wk-broken-doe"
    bad_dir.mkdir(parents=True)
    (bad_dir / "record.json").write_text("{not-json", encoding="utf-8")

    from tesseract.orchestrator.recovery.manager import new_recovery_manager

    manager = new_recovery_manager(tesseract_home=recovery_home)
    summary = await manager.run(emit_event=False)

    workers = summary.scans.get("workers", {})
    assert workers.get("interrupted", 0) == 1


@pytest.mark.asyncio
async def test_scan_is_idempotent(recovery_home: Path) -> None:
    """Two runs on the same fixture: the first archives terminals out
    of active/, the second sees zero work to do. Counts converge."""
    write_record(make_record(kind=WorkerKind.CLAUDE_CLI, status=WorkerStatus.RUNNING, agenda_item_id="ag-1"))

    from tesseract.orchestrator.recovery.manager import new_recovery_manager

    manager = new_recovery_manager(tesseract_home=recovery_home)
    first = await manager.run(emit_event=False)
    second = await manager.run(emit_event=False)

    assert first.scans.get("workers", {}).get("interrupted", 0) == 1
    # After first run, the interrupted record archived → active/ empty.
    assert second.scans.get("workers", {}).get("interrupted", 0) == 0
