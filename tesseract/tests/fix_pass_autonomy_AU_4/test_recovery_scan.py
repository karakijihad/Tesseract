"""AU-4 S2 — RecoveryManager scan 5 (agenda) integration.

Tests the runtime path: real AgendaStore + real WorkerRecord under a
monkeypatched TESSERACT_HOME; the recovery manager walks them and
applies transitions per ``_shared/recovery-state-machine.md §5``.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.delenv("TESSERACT_RESUME_CONTINUATION", raising=False)
    import tesseract.paths
    importlib.reload(tesseract.paths)
    import tesseract.orchestrator.recovery.manager as rm_mod
    importlib.reload(rm_mod)
    return tmp_path


def _seed_agenda_running_with_interrupted_worker(home: Path) -> tuple[str, str]:
    """Drop a running agenda item linked to a worker recorded as
    INTERRUPTED on disk. Returns ``(agenda_id, worker_id)``."""
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import (
        AgendaItem,
        AgendaSource,
        AgendaStatus,
        RiskClass,
        mint_agenda_id,
    )
    from tesseract.orchestrator.workers.kinds import WorkerKind
    from tesseract.orchestrator.workers.record import (
        RiskClass as WRiskClass,
        WorkerRecord,
        WorkerStatus,
        mint_worker_id,
        write_record,
    )

    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    worker_id = mint_worker_id(WorkerKind.TARS_SELF, now=now)
    record = WorkerRecord(
        id=worker_id,
        kind=WorkerKind.TARS_SELF,
        created_at=now,
        updated_at=now,
        agenda_item_id="placeholder",
        risk_class=WRiskClass.AUTONOMOUS,
        role="research-doe",
        status=WorkerStatus.INTERRUPTED,
    )
    write_record(record)

    item_id = mint_agenda_id("audit-doe-flow", now=now)
    item = AgendaItem(
        id=item_id,
        created_at=now,
        updated_at=now,
        source=AgendaSource.SELF_REFLECTION,
        goal="audit-doe-flow",
        risk_class=RiskClass.AUTONOMOUS,
        linked_workers=[worker_id],
        status=AgendaStatus.RUNNING,
    )
    AgendaStore().add(item)
    return item_id, worker_id


@pytest.mark.asyncio
async def test_scan5_no_items_zero_counts(isolated_home: Path) -> None:
    """Clean home → agenda scan emits the canonical empty shape."""
    from tesseract.orchestrator.recovery import new_recovery_manager
    rm = new_recovery_manager(tesseract_home=isolated_home)
    summary = await rm.run(emit_event=False)
    assert summary.scans["agenda"]["preserved"] == 0
    assert summary.scans["agenda"]["resume_queued"] == 0
    assert summary.scans["agenda"]["blocked"] == 0


@pytest.mark.asyncio
async def test_scan5_transitions_running_with_interrupted_worker_to_resume_queued(
    isolated_home: Path,
) -> None:
    item_id, worker_id = _seed_agenda_running_with_interrupted_worker(isolated_home)
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import AgendaStatus
    from tesseract.orchestrator.recovery import new_recovery_manager

    rm = new_recovery_manager(tesseract_home=isolated_home)
    summary = await rm.run(emit_event=False)

    assert summary.scans["agenda"]["resume_queued"] == 1
    item = AgendaStore().get(item_id)
    assert item is not None
    assert item.status == AgendaStatus.RESUME_QUEUED
    # Most recent transition was recorded as `by=recovery`.
    assert item.status_history[-1].by == "recovery"
    assert item.status_history[-1].reason == "agenda_resume"

    attn = [a for a in summary.operator_attention if a.id == item_id]
    assert attn and attn[0].reason == "agenda_resume"


@pytest.mark.asyncio
async def test_scan5_preserves_awaiting_operator(isolated_home: Path) -> None:
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import (
        AgendaItem,
        AgendaSource,
        AgendaStatus,
        RiskClass,
    )
    from tesseract.orchestrator.recovery import new_recovery_manager

    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    item = AgendaItem(
        id="ag-2026-05-18-1200-awaiting",
        created_at=now,
        updated_at=now,
        source=AgendaSource.OPERATOR,
        goal="needs operator look",
        risk_class=RiskClass.PROPOSE,
        status=AgendaStatus.AWAITING_OPERATOR,
    )
    AgendaStore().add(item)

    rm = new_recovery_manager(tesseract_home=isolated_home)
    summary = await rm.run(emit_event=False)
    # Status untouched, item counted in preserved bucket and surfaced.
    persisted = AgendaStore().get(item.id)
    assert persisted is not None
    assert persisted.status == AgendaStatus.AWAITING_OPERATOR
    assert summary.scans["agenda"]["preserved"] == 1
    attn = [a for a in summary.operator_attention if a.id == item.id]
    assert attn and attn[0].reason == "awaiting_operator_at_restart"


@pytest.mark.asyncio
async def test_scan5_idempotent(isolated_home: Path) -> None:
    """Running recovery twice on the same state produces the same final
    statuses — required by ``_shared/recovery-state-machine.md §Idempotency``."""
    item_id, _ = _seed_agenda_running_with_interrupted_worker(isolated_home)
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import AgendaStatus
    from tesseract.orchestrator.recovery import new_recovery_manager

    rm = new_recovery_manager(tesseract_home=isolated_home)
    await rm.run(emit_event=False)
    snapshot_after_first = AgendaStore().get(item_id)
    assert snapshot_after_first is not None
    first_status = snapshot_after_first.status
    first_history_len = len(snapshot_after_first.status_history)

    summary2 = await rm.run(emit_event=False)
    item_after_second = AgendaStore().get(item_id)
    assert item_after_second is not None
    assert item_after_second.status == first_status == AgendaStatus.RESUME_QUEUED
    # Second pass must NOT append a duplicate transition.
    assert len(item_after_second.status_history) == first_history_len
    # Second pass counts the resume_queued item but doesn't re-transition.
    assert summary2.scans["agenda"]["resume_queued"] == 1


@pytest.mark.asyncio
async def test_scan5_with_no_linked_workers_preserved(isolated_home: Path) -> None:
    """Selected item with no linked workers (kernel picked it but never
    dispatched) → preserved. Kernel re-selects on next cycle."""
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import (
        AgendaItem,
        AgendaSource,
        AgendaStatus,
        RiskClass,
    )
    from tesseract.orchestrator.recovery import new_recovery_manager

    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    item = AgendaItem(
        id="ag-2026-05-18-1200-selected",
        created_at=now,
        updated_at=now,
        source=AgendaSource.PROVIDER_WATCH,
        goal="selected but not yet dispatched",
        risk_class=RiskClass.AUTONOMOUS,
        status=AgendaStatus.SELECTED,
    )
    AgendaStore().add(item)

    rm = new_recovery_manager(tesseract_home=isolated_home)
    summary = await rm.run(emit_event=False)
    persisted = AgendaStore().get(item.id)
    assert persisted is not None
    assert persisted.status == AgendaStatus.SELECTED
    assert summary.scans["agenda"]["preserved"] == 1
