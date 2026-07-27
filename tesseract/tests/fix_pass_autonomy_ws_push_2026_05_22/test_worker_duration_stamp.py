"""Phase 7 — WorkerRecord.transition_to stamps duration_seconds on terminal.

The duration_seconds field on WorkerRecord was declared but never set in
production code; failed workers showed 0.0 in the Autonomy Workers pane
even when they ran for 5 minutes before timing out. The transition now
computes (updated_at - created_at).total_seconds() on every terminal
transition so the operator sees true lived time.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.workers.record import (
    RiskClass,
    StatusTransition,
    WorkerKind,
    WorkerRecord,
    WorkerStatus,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


def _make_record_created_n_seconds_ago(seconds_ago: float) -> WorkerRecord:
    created = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return WorkerRecord(
        id=f"wk-test-{int(seconds_ago)}",
        kind=WorkerKind.CLAUDE_CLI,
        created_at=created,
        updated_at=created,
        agenda_item_id="ag-test",
        risk_class=RiskClass.AUTONOMOUS,
        role="test",
        prompt="x",
        status=WorkerStatus.RUNNING,
        status_history=[
            StatusTransition(
                at=created, from_status="", to_status="running", reason="dispatch"
            )
        ],
    )


def test_transition_to_terminal_stamps_duration_seconds() -> None:
    record = _make_record_created_n_seconds_ago(300.0)
    assert record.duration_seconds == 0.0

    record.transition_to(WorkerStatus.FAILED, reason="tool_error")

    # ~300s elapsed — allow a generous tolerance for test scheduling.
    assert 295.0 <= record.duration_seconds <= 320.0


def test_transition_to_non_terminal_leaves_duration_unchanged() -> None:
    record = _make_record_created_n_seconds_ago(60.0)
    # SPAWNING is not in TERMINAL_STATUSES.
    record.transition_to(WorkerStatus.SPAWNING, reason="dispatch_2")
    assert record.duration_seconds == 0.0


def test_transition_to_same_status_no_duration_change() -> None:
    record = _make_record_created_n_seconds_ago(120.0)
    record.transition_to(WorkerStatus.RUNNING, reason="noop")
    assert record.duration_seconds == 0.0


def test_already_stamped_duration_not_overwritten() -> None:
    """Defensive: if a caller explicitly set duration_seconds (e.g. a
    delegate worker computing its own runtime), don't clobber it."""
    record = _make_record_created_n_seconds_ago(10.0)
    record.duration_seconds = 7.5

    record.transition_to(WorkerStatus.DONE, reason="completed")
    assert record.duration_seconds == 7.5
