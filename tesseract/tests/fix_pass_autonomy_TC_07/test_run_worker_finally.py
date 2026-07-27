"""HUD runs-surface fix (change-set E) — ``_run_worker`` must remove the
activity record even when the task is cancelled mid-flight.

Before the fix, ``remove_autonomy(record.agenda_item_id)`` ran as the last
statement of ``_run_worker`` — not in a ``finally:``. Task cancellation
(shutdown) or a hung/raising path above it left the record ``running``
forever; ``sweep_terminal_ephemeral`` only evicts terminal states, so a
stuck ``running`` record was never cleared. This test cancels the worker
task mid-await and asserts ``remove_autonomy`` still fires.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy import kernel as kernel_mod
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.kernel import AutonomyKernel
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.orchestrator.workers.record import (
    RiskClass,
    WorkerRecord,
    WorkerStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _HangingRunner:
    """A runner whose ``run`` never completes on its own — only cancellation
    ends it. Simulates a stuck/long-running worker or a shutdown mid-run."""

    async def run(self, record: Any) -> None:
        await asyncio.Event().wait()


class _FailingRunner:
    """A runner that marks the record FAILED and returns normally —
    simulates a worker whose CLI/API call errored out cleanly (no raise)."""

    async def run(self, record: Any) -> None:
        record.error_message = "boom: subprocess exit 1"
        record.transition_to(WorkerStatus.FAILED, reason="test_failure")


@pytest.mark.asyncio
async def test_run_worker_removes_activity_record_when_cancelled(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AgendaStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane.from_mission_lanes_block({}),
    )
    kernel._runner = _HangingRunner()

    removed: list[str] = []
    monkeypatch.setattr(
        kernel_mod, "remove_autonomy", lambda item_id: removed.append(item_id)
    )

    record = WorkerRecord(
        id="wk-cancel-1",
        kind=WorkerKind.CLAUDE_CLI,
        created_at=_now(),
        updated_at=_now(),
        agenda_item_id="ag-cancel-1",
        risk_class=RiskClass.OPERATOR_GATE,
        role="advisor",
        prompt="prompt",
        status=WorkerStatus.SPAWNING,
    )

    task = asyncio.create_task(kernel._run_worker(record))
    await asyncio.sleep(0)  # let the task start and hit the hanging await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert removed == ["ag-cancel-1"], (
        "remove_autonomy must fire from a finally: even when the worker "
        "task is cancelled mid-run"
    )


@pytest.mark.asyncio
async def test_run_worker_fails_autonomy_instead_of_removing_on_failed_status(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-07-05: the operator must not lose a FAILED worker to a silent
    chip disappearance — ``_run_worker`` must call ``fail_autonomy`` (not
    ``remove_autonomy``) when the record's terminal status is FAILED."""
    store = AgendaStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane.from_mission_lanes_block({}),
    )
    kernel._runner = _FailingRunner()

    removed: list[str] = []
    failed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        kernel_mod, "remove_autonomy", lambda item_id: removed.append(item_id)
    )
    monkeypatch.setattr(
        kernel_mod, "fail_autonomy",
        lambda item_id, *, detail: failed.append((item_id, detail)),
    )

    record = WorkerRecord(
        id="wk-fail-1",
        kind=WorkerKind.CLAUDE_CLI,
        created_at=_now(),
        updated_at=_now(),
        agenda_item_id="ag-fail-1",
        risk_class=RiskClass.OPERATOR_GATE,
        role="advisor",
        prompt="prompt",
        status=WorkerStatus.SPAWNING,
    )

    await kernel._run_worker(record)

    assert failed == [("ag-fail-1", "boom: subprocess exit 1")], (
        "a FAILED worker must transition its activity chip via fail_autonomy, "
        "carrying the worker's error_message as the detail"
    )
    assert removed == [], "remove_autonomy must not fire for a FAILED worker"
