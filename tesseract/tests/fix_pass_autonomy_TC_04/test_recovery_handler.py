"""``TarsControllerRecoveryHandler.can_recover`` + ``resume`` + classify_recovery_reason."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import RiskClass
from tesseract.orchestrator.tars_controller import TarsControllerRecoveryHandler
from tesseract.orchestrator.workers.heartbeat import STALENESS_THRESHOLD_SECONDS
from tesseract.orchestrator.workers.recovery import classify_recovery_reason
from tesseract.orchestrator.workers.record import WorkerRecord, WorkerStatus


def _fresh_record(
    home: Path, *, pid: int, hb_age_seconds: float = 0.0
) -> WorkerRecord:
    hb_path = home / "tars_controller" / "ctrl-x" / "heartbeat"
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    hb_path.touch()
    if hb_age_seconds:
        ts = time.time() - hb_age_seconds
        os.utime(hb_path, (ts, ts))
    now = datetime.now(timezone.utc)
    return WorkerRecord(
        id="wrk-test-1",
        kind=WorkerKind.TARS_CONTROLLER,
        created_at=now,
        updated_at=now,
        agenda_item_id="agenda-1",
        risk_class=RiskClass.OPERATOR_GATE,
        role="",
        status=WorkerStatus.RUNNING,
        controller_id="ctrl-x",
        controller_pid=pid,
        controller_hb_path=str(hb_path),
        session_id="2026-05-23-deadbeef",
    )


def test_can_recover_returns_false_when_pid_missing(isolated_home: Path) -> None:
    record = _fresh_record(isolated_home, pid=1)
    record.controller_pid = None
    handler = TarsControllerRecoveryHandler()
    assert handler.can_recover(record) is False


def test_can_recover_returns_false_when_hb_path_missing(isolated_home: Path) -> None:
    record = _fresh_record(isolated_home, pid=os.getpid())
    record.controller_hb_path = None
    handler = TarsControllerRecoveryHandler()
    assert handler.can_recover(record) is False


def test_can_recover_returns_false_when_hb_stale(isolated_home: Path) -> None:
    record = _fresh_record(
        isolated_home,
        pid=os.getpid(),
        hb_age_seconds=STALENESS_THRESHOLD_SECONDS + 30,
    )
    handler = TarsControllerRecoveryHandler()
    assert handler.can_recover(record) is False


def test_can_recover_true_when_alive_and_fresh(isolated_home: Path) -> None:
    record = _fresh_record(isolated_home, pid=os.getpid())
    handler = TarsControllerRecoveryHandler()
    assert handler.can_recover(record) is True


@pytest.mark.asyncio
async def test_resume_transitions_to_running(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The handler calls write_record which writes under TESSERACT_HOME; the
    # fixture already redirects writes off the production tree.
    record = _fresh_record(isolated_home, pid=os.getpid())
    # _fresh_record builds a record in RUNNING via the default; bump it to
    # SPAWNING so the resume transition is observable.
    record.status = WorkerStatus.SPAWNING
    handler = TarsControllerRecoveryHandler()
    updated = await handler.resume(record)
    assert updated.status == WorkerStatus.RUNNING
    assert any(
        t.reason == "reattached_after_restart" for t in updated.status_history
    )


def test_classify_recovery_reason_for_controller_kind_is_worker_lost(
    isolated_home: Path,
) -> None:
    """TARS_CONTROLLER is NOT in the PTY-bound set; a lost controller =
    REASON_WORKER_LOST, not REASON_PANE_LOST."""
    from tesseract.orchestrator.recovery.transitions import REASON_WORKER_LOST
    from tesseract.orchestrator.workers.heartbeat import touch_heartbeat

    record = _fresh_record(isolated_home, pid=os.getpid())
    # Touch the worker heartbeat so we don't trip the stale branch first;
    # we're testing the kind classifier, not the staleness path.
    touch_heartbeat(record.id)
    assert classify_recovery_reason(record) == REASON_WORKER_LOST
