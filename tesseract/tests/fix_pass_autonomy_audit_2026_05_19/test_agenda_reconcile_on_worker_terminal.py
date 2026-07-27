"""Codex audit 2026-05-19 P0 #2 — kernel must reconcile the linked
agenda item when its worker reaches a terminal state.

Prior bug: ``AgendaItem`` stayed ``status=running`` after the linked
``WorkerRecord`` transitioned to ``FAILED`` (or any other terminal).
The dashboard then showed RUNNING autonomy linked to dead workers —
TARS looked busy while doing nothing.

Fix: ``AutonomyKernel._run_worker`` now reads the terminal status off
the in-place-mutated record and transitions the linked agenda item
via ``_reconcile_agenda_for_worker``:

* ``WorkerStatus.DONE``      → ``AgendaStatus.DONE``
* every other terminal       → ``AgendaStatus.BLOCKED`` with
  ``blocked_reason=worker_<status>:<id>``
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy import (
    AgendaStore,
    AutonomyKernel,
    KernelConfig,
    MapperConfig,
)
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)
from tesseract.orchestrator.autonomy.worker_dispatch import WorkerRunner
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.orchestrator.workers.record import (
    WorkerRecord,
    WorkerStatus,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def store(isolated_home: Path) -> AgendaStore:
    return AgendaStore()


def _make_running_item(store: AgendaStore, worker_id: str) -> AgendaItem:
    """Create a RUNNING agenda item linked to ``worker_id``."""
    item = AgendaItem(
        id=mint_agenda_id(AgendaSource.PROVIDER_WATCH),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        source=AgendaSource.PROVIDER_WATCH,
        goal="test goal",
        risk_class=RiskClass.AUTONOMOUS,
        status=AgendaStatus.RUNNING,
        linked_workers=[worker_id],
    )
    store.save(item)
    return item


def _kernel(store: AgendaStore) -> AutonomyKernel:
    return AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane({WorkerKind.TARS_SELF: 10}),
        config=KernelConfig(top_k=3, max_concurrent_workers_total=8),
        mapper_configs={},
    )


def _record(item_id: str, *, status: WorkerStatus) -> WorkerRecord:
    moment = datetime.now(timezone.utc)
    return WorkerRecord(
        id=f"wk-test-{status.value}",
        kind=WorkerKind.TARS_SELF,
        created_at=moment,
        updated_at=moment,
        agenda_item_id=item_id,
        risk_class=RiskClass.AUTONOMOUS,
        role="",
        prompt="test",
        status=status,
    )


def test_done_worker_transitions_agenda_done(store: AgendaStore) -> None:
    kernel = _kernel(store)
    item = _make_running_item(store, worker_id="wk-test-done")
    record = _record(item.id, status=WorkerStatus.DONE)

    kernel._reconcile_agenda_for_worker(record)

    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.DONE


def test_failed_worker_transitions_agenda_blocked_with_reason(store: AgendaStore) -> None:
    kernel = _kernel(store)
    item = _make_running_item(store, worker_id="wk-test-failed")
    record = _record(item.id, status=WorkerStatus.FAILED)

    kernel._reconcile_agenda_for_worker(record)

    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.BLOCKED
    assert refreshed.blocked_reason == f"worker_failed:{record.id}"


def test_interrupted_worker_blocks_with_reason(store: AgendaStore) -> None:
    kernel = _kernel(store)
    item = _make_running_item(store, worker_id="wk-test-interrupted")
    record = _record(item.id, status=WorkerStatus.INTERRUPTED)

    kernel._reconcile_agenda_for_worker(record)

    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.BLOCKED
    assert refreshed.blocked_reason == f"worker_interrupted:{record.id}"


def test_reconcile_skipped_when_agenda_already_terminal(store: AgendaStore) -> None:
    """Operator-driven terminals (cancelled / awaiting_operator-transitioned-
    to-done) must NOT be overridden by the worker terminal — the operator
    surface owns those transitions."""
    kernel = _kernel(store)
    # Build a CANCELLED item linked to the worker
    item = AgendaItem(
        id=mint_agenda_id(AgendaSource.PROVIDER_WATCH),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        source=AgendaSource.PROVIDER_WATCH,
        goal="cancelled by operator",
        risk_class=RiskClass.AUTONOMOUS,
        status=AgendaStatus.CANCELLED,
        linked_workers=["wk-test-failed"],
    )
    store.save(item)
    record = _record(item.id, status=WorkerStatus.FAILED)

    kernel._reconcile_agenda_for_worker(record)

    # Cancelled items archive on save; pull from archive too via get()
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.CANCELLED
    assert refreshed.blocked_reason is None


def test_reconcile_no_op_on_missing_agenda_item(store: AgendaStore, caplog) -> None:
    """Worker references an agenda item that's gone — log a warning,
    don't raise."""
    kernel = _kernel(store)
    record = _record("ag-does-not-exist", status=WorkerStatus.FAILED)
    # Must not raise
    kernel._reconcile_agenda_for_worker(record)


def test_reconcile_no_op_on_empty_agenda_item_id(store: AgendaStore) -> None:
    """Some kernel paths spawn workers with no linked agenda item.
    Reconcile must early-return cleanly."""
    kernel = _kernel(store)
    record = _record("", status=WorkerStatus.DONE)
    record.agenda_item_id = ""  # type: ignore[misc]
    kernel._reconcile_agenda_for_worker(record)  # no raise = pass


class _FakeRunner:
    """Runner that transitions the record to FAILED without doing real work."""

    def __init__(self, terminal: WorkerStatus = WorkerStatus.FAILED) -> None:
        self._terminal = terminal

    async def run(self, record: WorkerRecord) -> None:
        record.transition_to(self._terminal, reason="fake_runner")


async def test_run_worker_reconciles_after_runner_returns(store: AgendaStore) -> None:
    """End-to-end: kernel._run_worker delegates to the runner, then
    reconciles the linked agenda item once the runner returns."""
    item = _make_running_item(store, worker_id="wk-end-to-end")
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane({WorkerKind.TARS_SELF: 10}),
        config=KernelConfig(top_k=3, max_concurrent_workers_total=8),
        mapper_configs={},
        worker_runner=_FakeRunner(terminal=WorkerStatus.FAILED),
    )
    record = _record(item.id, status=WorkerStatus.RUNNING)
    record.id = "wk-end-to-end"

    await kernel._run_worker(record)

    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.BLOCKED
    assert refreshed.blocked_reason == "worker_failed:wk-end-to-end"
