"""Phase 3 — worker_record_* WS envelopes fired from write_record + archive_record.

The autonomy kernel + governor + cancel/recovery paths each mutate worker
records via module-level ``write_record`` / ``archive_record``. Those
mutations were silent to WS subscribers — the operator's Autonomy tab
showed stale ``Workers`` state until a manual refresh. The record writers
now fire a process-wide ``fire_worker_broadcast`` hook on every successful
write so the Mirror can fan ``worker_record_*`` envelopes.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.workers import broadcast as worker_broadcast
from tesseract.orchestrator.workers.record import (
    StatusTransition,
    WorkerKind,
    WorkerRecord,
    WorkerStatus,
    archive_record,
    mint_worker_id,
    write_record,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    # Reset the global hook between tests so leftovers don't leak across cases.
    worker_broadcast.set_worker_broadcast_hook(None)
    return tmp_path


def _make_record(*, status: WorkerStatus = WorkerStatus.QUEUED) -> WorkerRecord:
    from tesseract.orchestrator.workers.record import RiskClass

    now = datetime.now(timezone.utc)
    record = WorkerRecord(
        id=mint_worker_id(WorkerKind.CLAUDE_CLI, now=now),
        kind=WorkerKind.CLAUDE_CLI,
        created_at=now,
        updated_at=now,
        agenda_item_id="ag-test-001",
        risk_class=RiskClass.AUTONOMOUS,
        role="test",
        prompt="test prompt",
        status=status,
        status_history=[
            StatusTransition(
                at=now, from_status="", to_status=status.value, reason="created"
            )
        ],
    )
    return record


def test_first_write_fires_started_event() -> None:
    calls: list[tuple[str, str]] = []

    def _hook(event_type, record):
        calls.append((event_type, record.id))

    worker_broadcast.set_worker_broadcast_hook(_hook)

    record = _make_record()
    write_record(record)

    assert len(calls) == 1
    assert calls[0][0] == "worker_record_started"
    assert calls[0][1] == record.id


def test_subsequent_write_fires_transitioned_event() -> None:
    calls: list[tuple[str, str]] = []

    def _hook(event_type, record):
        calls.append((event_type, record.id))

    worker_broadcast.set_worker_broadcast_hook(_hook)

    record = _make_record()
    write_record(record)
    calls.clear()

    record.transition_to(WorkerStatus.SPAWNING, reason="kernel_dispatch")
    write_record(record)

    assert len(calls) == 1
    assert calls[0][0] == "worker_record_transitioned"


def test_archive_fires_archived_event() -> None:
    calls: list[tuple[str, str]] = []

    def _hook(event_type, record):
        calls.append((event_type, record.id))

    worker_broadcast.set_worker_broadcast_hook(_hook)

    record = _make_record()
    write_record(record)
    record.transition_to(WorkerStatus.DONE, reason="completed")
    write_record(record)
    calls.clear()

    archive_record(record)
    archived_events = [c for c in calls if c[0] == "worker_record_archived"]
    assert len(archived_events) == 1


def test_no_hook_set_silent_writes() -> None:
    # Default state: hook is unset; writes must succeed without raising
    # and without any side-effect beyond the disk write.
    record = _make_record()
    write_record(record)
    record.transition_to(WorkerStatus.DONE, reason="completed")
    write_record(record)
    archive_record(record)
    # Sanity: the record landed on disk under archive bucket.
    from tesseract.orchestrator.workers.record import load_record

    loaded = load_record(record.id)
    assert loaded is not None
    assert loaded.status == WorkerStatus.DONE


def test_hook_exception_does_not_break_write() -> None:
    def _bad_hook(event_type, record):
        raise RuntimeError("hook intentionally raises")

    worker_broadcast.set_worker_broadcast_hook(_bad_hook)

    record = _make_record()
    # write_record must succeed even though the hook explodes.
    write_record(record)
    from tesseract.orchestrator.workers.record import load_record

    loaded = load_record(record.id)
    assert loaded is not None
    assert loaded.id == record.id


def test_invalid_event_type_is_dropped() -> None:
    """Defensive: fire_worker_broadcast must reject typos against its allowlist."""
    calls: list[tuple[str, str]] = []

    def _hook(event_type, record):
        calls.append((event_type, record.id))

    worker_broadcast.set_worker_broadcast_hook(_hook)

    record = _make_record()
    worker_broadcast.fire_worker_broadcast("totally_made_up", record)
    assert calls == []
