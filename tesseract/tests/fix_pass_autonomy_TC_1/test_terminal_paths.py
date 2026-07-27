"""TC-1 — outcome rows fire on every terminal worker path, not just
the kernel-reconcile flow.

Reviewer H-1: governor cost-spiral cancel writes the journal row.
Reviewer H-2: recovery mark_interrupted writes the journal row.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.governor import (
    Governor,
    GovernorConfig,
    GovernorTickResult,
    PauseStore,
)
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
    WorkerRecord,
    WorkerStatus,
    write_record,
)
from tesseract.orchestrator.workers.recovery import _InterruptOnlyHandler


def _read_today(home: Path) -> list[dict]:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = home / "operator_journal" / f"{day}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_recovery_mark_interrupted_writes_outcome(
    isolated_home: Path,
) -> None:
    """Backend restart drives orphan workers through `mark_interrupted`;
    the operator journal must record the INTERRUPTED terminal so the
    operator sees what the recovery sweep killed."""
    now = datetime.now(timezone.utc)
    record = WorkerRecord(
        id="wk-doe-interrupted",
        kind=WorkerKind.TARS_SELF,
        created_at=now,
        updated_at=now,
        agenda_item_id="ag-doe-interrupted",
        risk_class=RiskClass.PROPOSE,
        role="",
        status=WorkerStatus.RUNNING,
    )
    write_record(record)
    handler = _InterruptOnlyHandler(WorkerKind.TARS_SELF)
    await handler.mark_interrupted(record, reason="boot_recovery")

    rows = _read_today(isolated_home)
    outcomes = [r for r in rows if r["event_type"] == "outcome"]
    assert len(outcomes) == 1
    row = outcomes[0]
    assert row["worker_id"] == "wk-doe-interrupted"
    assert row["agenda_item_id"] == "ag-doe-interrupted"
    assert row["status"] == "interrupted"
    assert row["summary"] == "boot_recovery"


def test_governor_cost_spiral_cancel_writes_outcome(
    isolated_home: Path,
) -> None:
    """Governor-driven worker cancel must surface in the journal so the
    operator sees that an out-of-budget worker was killed."""
    now = datetime.now(timezone.utc)
    store = AgendaStore()
    item = AgendaItem(
        id=mint_agenda_id("doe-spiral", now=now),
        created_at=now,
        updated_at=now,
        source=AgendaSource.OPERATOR,
        goal="doe spiral goal",
        risk_class=RiskClass.PROPOSE,
    )
    store.add(item)

    record = WorkerRecord(
        id="wk-doe-spiral",
        kind=WorkerKind.TARS_SELF,
        created_at=now,
        updated_at=now,
        agenda_item_id=item.id,
        risk_class=RiskClass.PROPOSE,
        role="",
        status=WorkerStatus.RUNNING,
    )
    write_record(record)

    governor = Governor(
        agenda_store=store,
        pause_store=PauseStore(),
        config=GovernorConfig(),
    )
    result = GovernorTickResult()
    governor._cancel_worker_and_block_item(record.id, item.id, result)

    rows = _read_today(isolated_home)
    outcomes = [r for r in rows if r["event_type"] == "outcome"]
    assert len(outcomes) == 1
    row = outcomes[0]
    assert row["worker_id"] == "wk-doe-spiral"
    assert row["status"] == "cancelled"
    assert row["summary"] == "governor_cost_spiral"
