"""Shared fixtures for AU-3 worker substrate tests.

All tests in this folder MUST run under a monkeypatched
``TESSERACT_HOME`` so the production ``tesseract/workers/`` and
``tesseract/logs/`` trees stay untouched. The worker substrate
resolves ``TESSERACT_HOME`` at call time (see
``orchestrator/workers/paths.py``), so a single ``setenv`` is enough —
no module reload is needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    RiskClass,
    WorkerRecord,
    WorkerStatus,
    mint_worker_id,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def make_record(
    *,
    kind: WorkerKind = WorkerKind.TARS_SELF,
    risk_class: RiskClass = RiskClass.AUTONOMOUS,
    status: WorkerStatus = WorkerStatus.QUEUED,
    agenda_item_id: str = "ag-doe-001",
    role: str = "research-doe",
    worker_id: str | None = None,
    now: datetime | None = None,
) -> WorkerRecord:
    """Build a WorkerRecord for tests. Defaults use Jane/John Doe-style
    fixture values so production state can never accidentally inherit
    them. Uses the live mint helper so id format stays in sync."""
    when = now or datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    wid = worker_id or mint_worker_id(kind, now=when)
    return WorkerRecord(
        id=wid,
        kind=kind,
        created_at=when,
        updated_at=when,
        agenda_item_id=agenda_item_id,
        risk_class=risk_class,
        role=role,
        status=status,
    )


__all__ = ["isolated_home", "make_record"]
