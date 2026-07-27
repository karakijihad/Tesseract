"""Codex audit-2 2026-05-19 P2 — kernel one-shot stale-agenda repair.

The in-process reconciler in ``_run_worker`` (audit-1 P0 #2) only
catches workers terminating going forward. Items left in RUNNING /
SELECTED on disk from pre-fix builds need a separate sweep on boot.

``AutonomyKernel.repair_stale_agenda_items`` runs that sweep:

* walks ``AgendaStore.iter_active()`` filtered to RUNNING/SELECTED
* for each, loads every linked worker's record
* if ALL linked workers are terminal → reconcile the item
  (DONE if all DONE; BLOCKED with structured reason otherwise)
* items still linked to in-flight workers are left alone — the
  in-process reconciler will close them on terminal
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy import (
    AgendaStore,
    AutonomyKernel,
    KernelConfig,
)
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.orchestrator.workers.record import (
    WorkerRecord,
    WorkerStatus,
    write_record,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _kernel(store: AgendaStore) -> AutonomyKernel:
    return AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane({WorkerKind.TARS_SELF: 10}),
        config=KernelConfig(top_k=3, max_concurrent_workers_total=8),
        mapper_configs={},
    )


def _running_item(store: AgendaStore, *, linked: list[str]) -> AgendaItem:
    moment = datetime.now(timezone.utc)
    item = AgendaItem(
        id=mint_agenda_id(AgendaSource.PROVIDER_WATCH),
        created_at=moment,
        updated_at=moment,
        source=AgendaSource.PROVIDER_WATCH,
        goal="stale test",
        risk_class=RiskClass.AUTONOMOUS,
        status=AgendaStatus.RUNNING,
        linked_workers=linked,
    )
    store.save(item)
    return item


def _terminal_worker(item_id: str, *, status: WorkerStatus, wid: str) -> None:
    moment = datetime.now(timezone.utc)
    rec = WorkerRecord(
        id=wid,
        kind=WorkerKind.TARS_SELF,
        created_at=moment,
        updated_at=moment,
        agenda_item_id=item_id,
        risk_class=RiskClass.AUTONOMOUS,
        role="",
        prompt="x",
        status=WorkerStatus.SPAWNING,
    )
    write_record(rec)
    # Transition to terminal; persist again to land in archive.
    rec.transition_to(status, reason="test_stale")
    write_record(rec)


def _unvetted_item(store: AgendaStore, *, created_at: datetime) -> AgendaItem:
    item = AgendaItem(
        id=mint_agenda_id(AgendaSource.SELF_REFLECTION),
        created_at=created_at,
        updated_at=created_at,
        source=AgendaSource.SELF_REFLECTION,
        goal="unvetted staleness test",
        risk_class=RiskClass.PROPOSE,
        status=AgendaStatus.UNVETTED,
    )
    store.save(item)
    return item


def test_repair_promotes_stale_unvetted_item(isolated_home: Path) -> None:
    """Fix 1 — escape valve: an UNVETTED item stuck past
    ``max_unvetted_hours`` (default 24) promotes to PROPOSED on repair,
    even though nothing else (e.g. a disabled autonomy_vetter job)
    would otherwise touch it."""
    store = AgendaStore()
    stale_created = datetime.now(timezone.utc) - timedelta(hours=25)
    item = _unvetted_item(store, created_at=stale_created)
    kernel = _kernel(store)

    summary = kernel.repair_stale_agenda_items()

    assert summary["unvetted_promoted"] == 1
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.PROPOSED


def test_repair_leaves_fresh_unvetted_item_alone(isolated_home: Path) -> None:
    store = AgendaStore()
    fresh_created = datetime.now(timezone.utc) - timedelta(hours=1)
    item = _unvetted_item(store, created_at=fresh_created)
    kernel = _kernel(store)

    summary = kernel.repair_stale_agenda_items()

    assert summary["unvetted_promoted"] == 0
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.UNVETTED


def test_repair_transitions_done_when_all_workers_done(isolated_home: Path) -> None:
    store = AgendaStore()
    item = _running_item(store, linked=["wk-stale-done"])
    _terminal_worker(item.id, status=WorkerStatus.DONE, wid="wk-stale-done")
    kernel = _kernel(store)

    summary = kernel.repair_stale_agenda_items()

    assert summary["reconciled_done"] == 1
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.DONE


def test_repair_transitions_blocked_when_a_worker_failed(isolated_home: Path) -> None:
    store = AgendaStore()
    item = _running_item(store, linked=["wk-stale-failed"])
    _terminal_worker(item.id, status=WorkerStatus.FAILED, wid="wk-stale-failed")
    kernel = _kernel(store)

    summary = kernel.repair_stale_agenda_items()

    assert summary["reconciled_blocked"] == 1
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.BLOCKED
    assert refreshed.blocked_reason is not None
    assert "stale_repair" in refreshed.blocked_reason
    assert "wk-stale-failed" in refreshed.blocked_reason


def test_repair_skips_item_with_in_flight_worker(isolated_home: Path) -> None:
    """If even one linked worker is still in-flight, the item is left
    alone — the in-process reconciler will close it when the worker
    terminates."""
    store = AgendaStore()
    item = _running_item(store, linked=["wk-stale-done", "wk-running"])
    _terminal_worker(item.id, status=WorkerStatus.DONE, wid="wk-stale-done")
    # ``wk-running`` is a non-terminal SPAWNING record on disk
    moment = datetime.now(timezone.utc)
    write_record(WorkerRecord(
        id="wk-running",
        kind=WorkerKind.TARS_SELF,
        created_at=moment,
        updated_at=moment,
        agenda_item_id=item.id,
        risk_class=RiskClass.AUTONOMOUS,
        role="",
        prompt="x",
        status=WorkerStatus.SPAWNING,
    ))
    kernel = _kernel(store)

    summary = kernel.repair_stale_agenda_items()

    assert summary["reconciled_done"] == 0
    assert summary["reconciled_blocked"] == 0
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.RUNNING


def test_repair_counts_items_with_no_linked_workers(isolated_home: Path) -> None:
    store = AgendaStore()
    item = _running_item(store, linked=[])
    kernel = _kernel(store)
    summary = kernel.repair_stale_agenda_items()
    assert summary["no_workers"] == 1
    assert summary["reconciled_done"] == 0
    refreshed = store.get(item.id)
    # No-workers items are not touched by repair — operator decides.
    assert refreshed.status == AgendaStatus.RUNNING


def test_repair_skips_item_with_missing_worker_record(isolated_home: Path) -> None:
    """Codex audit-2 reviewer follow-up: missing-worker policy must be
    conservative — if any linked worker can't be loaded, leave the item
    alone rather than synthesize completion from the loadable subset.
    A missing record may indicate corruption, not done-ness."""
    store = AgendaStore()
    item = _running_item(store, linked=["wk-stale-done", "wk-never-existed"])
    # Only the first worker has a record on disk; the second is missing.
    _terminal_worker(item.id, status=WorkerStatus.DONE, wid="wk-stale-done")
    kernel = _kernel(store)

    summary = kernel.repair_stale_agenda_items()

    assert summary["items_with_missing_worker"] == 1
    assert summary["reconciled_done"] == 0
    assert summary["reconciled_blocked"] == 0
    refreshed = store.get(item.id)
    assert refreshed is not None
    # Item is left in RUNNING — operator must triage the missing record.
    assert refreshed.status == AgendaStatus.RUNNING


def test_repair_is_idempotent(isolated_home: Path) -> None:
    """Running repair twice produces no extra transitions on the second
    pass — the items moved to terminal in pass 1 are no longer in
    iter_active and the helper skips them."""
    store = AgendaStore()
    item = _running_item(store, linked=["wk-stale-failed"])
    _terminal_worker(item.id, status=WorkerStatus.FAILED, wid="wk-stale-failed")
    kernel = _kernel(store)

    first = kernel.repair_stale_agenda_items()
    second = kernel.repair_stale_agenda_items()

    assert first["reconciled_blocked"] == 1
    assert second["reconciled_blocked"] == 0
