"""TC-3 exit criterion: CODEX_CLI no longer classifies as PANE_LOST."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.recovery.transitions import (
    REASON_PANE_LOST,
    REASON_WORKER_LOST,
)
from tesseract.orchestrator.workers.record import (
    RiskClass,
    WorkerRecord,
    WorkerStatus,
)
from tesseract.orchestrator.workers import recovery as recovery_mod


def _record(kind: WorkerKind) -> WorkerRecord:
    now = datetime.now(timezone.utc)
    return WorkerRecord(
        id=f"worker-{kind.value}",
        kind=kind,
        created_at=now,
        updated_at=now,
        agenda_item_id="ag-test",
        risk_class=RiskClass.PROPOSE,
        role="test",
        status=WorkerStatus.RUNNING,
    )


@pytest.fixture
def fresh_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass heartbeat-stale (file-based) so we exercise the kind branch."""
    monkeypatch.setattr(recovery_mod, "is_heartbeat_stale", lambda _wid: False)


def test_codex_cli_is_worker_lost_not_pane_lost(fresh_heartbeat: None) -> None:
    assert (
        recovery_mod.classify_recovery_reason(_record(WorkerKind.CODEX_CLI))
        == REASON_WORKER_LOST
    )


def test_claude_cli_remains_pane_lost(fresh_heartbeat: None) -> None:
    assert (
        recovery_mod.classify_recovery_reason(_record(WorkerKind.CLAUDE_CLI))
        == REASON_PANE_LOST
    )


def test_terminal_remains_pane_lost(fresh_heartbeat: None) -> None:
    assert (
        recovery_mod.classify_recovery_reason(_record(WorkerKind.TERMINAL))
        == REASON_PANE_LOST
    )
