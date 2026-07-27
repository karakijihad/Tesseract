"""Audit-2 M-3: ``_dispatch_item`` must allocate a worktree before
writing the SPAWNING worker record, and ``_run_worker`` must finalize it
on terminal status.

Before the fix, ``allocate_for_record`` lived only in helper-test code
paths — the autonomy kernel built the record and went straight to
``write_record`` with ``worktree_path = None`` even for CLAUDE_CLI/CODEX_CLI
work at PROPOSE/OPERATOR_GATE risk. Source-editing workers could run
inside the live tree, defeating the AU-12 isolation invariant.

The tests monkey-patch ``allocate_for_record`` / ``finalize_for_record``
in the kernel module namespace (the kernel binds them at import time so
that's the namespace patched) and verify they fire with the
``WorkerRecord`` the kernel just minted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tesseract.orchestrator.autonomy import kernel as kernel_mod
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.kernel import AutonomyKernel
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.orchestrator.workers.record import RiskClass, WorkerStatus
from tesseract.orchestrator.workers.worktree import WorktreeError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_operator_gate_item() -> AgendaItem:
    now = _now()
    return AgendaItem(
        id="ag-disp-1",
        created_at=now,
        updated_at=now,
        source=AgendaSource.OPERATOR,
        goal="patch the auth middleware",
        risk_class=RiskClass.OPERATOR_GATE,
        status=AgendaStatus.PROPOSED,
        priority_score=1.0,
    )


@pytest.mark.asyncio
async def test_dispatch_item_allocates_worktree_for_claude_cli(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AgendaStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane.from_mission_lanes_block({}),
    )
    item = _make_operator_gate_item()
    kernel._agenda.add(item, reason="test_seed")

    captured: list[Any] = []

    def _fake_allocate(record: Any) -> None:
        captured.append(record)
        # Mimic the real allocator: stamp a path on the record so the
        # SPAWNING write captures it.
        record.worktree_path = str(isolated_home / "wt" / record.id)

    monkeypatch.setattr(
        kernel_mod, "_allocate_worktree_for_record", _fake_allocate
    )

    # Block the runner from actually executing (we only care about
    # dispatch-time allocation).
    runner = AsyncMock()
    runner.run = AsyncMock(return_value=None)
    kernel._runner = runner

    # Stub rationale generation so the dispatch path doesn't pull in a model.
    async def _fake_rationale(*_a: Any, **_k: Any) -> str:
        return "test rationale"

    monkeypatch.setattr(kernel_mod, "generate_rationale", _fake_rationale)

    await kernel._dispatch_item(item, kind=WorkerKind.CLAUDE_CLI)
    # Drain the spawned task so finalize can run.
    for task in list(kernel._dispatch_tasks):
        await task

    assert len(captured) == 1, (
        "allocate_for_record must fire exactly once per dispatch"
    )
    record = captured[0]
    assert record.kind is WorkerKind.CLAUDE_CLI
    assert record.risk_class is RiskClass.OPERATOR_GATE


@pytest.mark.asyncio
async def test_run_worker_finalizes_worktree_on_terminal_status(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AgendaStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane.from_mission_lanes_block({}),
    )
    item = _make_operator_gate_item()
    kernel._agenda.add(item, reason="test_seed")

    finalize_calls: list[Any] = []

    def _fake_allocate(record: Any) -> None:
        record.worktree_path = str(isolated_home / "wt" / record.id)

    def _fake_finalize(record: Any) -> None:
        finalize_calls.append(record)

    monkeypatch.setattr(
        kernel_mod, "_allocate_worktree_for_record", _fake_allocate
    )
    monkeypatch.setattr(
        kernel_mod, "_finalize_worktree_for_record", _fake_finalize
    )

    async def _fake_rationale(*_a: Any, **_k: Any) -> str:
        return "test rationale"

    monkeypatch.setattr(kernel_mod, "generate_rationale", _fake_rationale)

    # The runner drives the record to DONE so finalize must fire.
    class _DoneRunner:
        async def run(self, record: Any) -> None:
            record.transition_to(WorkerStatus.DONE, reason="runner_done")

    kernel._runner = _DoneRunner()

    await kernel._dispatch_item(item, kind=WorkerKind.CLAUDE_CLI)
    for task in list(kernel._dispatch_tasks):
        await task

    assert len(finalize_calls) == 1, (
        "finalize_for_record must fire on terminal status"
    )
    assert finalize_calls[0].status is WorkerStatus.DONE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        WorktreeError("git worktree add failed (exit=128): fatal: ..."),
        FileNotFoundError(2, "No such file or directory", "git"),
        PermissionError(13, "Permission denied"),
    ],
    ids=["worktree_error", "git_missing", "permission_denied"],
)
async def test_dispatch_item_halts_in_blocked_when_allocation_fails(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
) -> None:
    """Audit-1 follow-up: worktree allocation failure is fail-closed.

    For code-editing kinds (claude_cli / codex_cli) at PROPOSE /
    OPERATOR_GATE risk, a WorktreeError / OSError out of
    ``allocate_for_record`` must transition the agenda item to BLOCKED
    with ``blocked_reason=worktree_alloc_failed:<class>:<exc>`` and the
    runner must NOT be spawned. Isolation is the whole point — the
    worker never runs against the live tree just because allocation
    tripped. The operator clears the underlying cause (install git,
    free disk, fix permissions) and unblocks for the next tick.
    """
    store = AgendaStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane.from_mission_lanes_block({}),
    )
    item = _make_operator_gate_item()
    kernel._agenda.add(item, reason="test_seed")

    def _raising_allocate(_record: Any) -> None:
        raise exc

    monkeypatch.setattr(
        kernel_mod, "_allocate_worktree_for_record", _raising_allocate
    )

    # Runner must never be invoked.
    runner_called = False

    class _AssertNotCalled:
        async def run(self, _record: Any) -> None:
            nonlocal runner_called
            runner_called = True

    kernel._runner = _AssertNotCalled()

    async def _fake_rationale(*_a: Any, **_k: Any) -> str:
        return "test rationale"

    monkeypatch.setattr(kernel_mod, "generate_rationale", _fake_rationale)

    await kernel._dispatch_item(item, kind=WorkerKind.CLAUDE_CLI)
    for task in list(kernel._dispatch_tasks):
        await task

    assert item.status is AgendaStatus.BLOCKED, (
        f"item must halt in BLOCKED on allocation failure, got {item.status}"
    )
    assert item.blocked_reason is not None
    assert item.blocked_reason.startswith("worktree_alloc_failed:"), (
        f"blocked_reason must encode the failure: {item.blocked_reason!r}"
    )
    assert type(exc).__name__ in item.blocked_reason
    assert not item.linked_workers, (
        "no worker record must be linked when dispatch halts at allocation"
    )
    assert not kernel._dispatch_tasks, (
        "no runner task must be spawned when allocation fails"
    )
    assert runner_called is False


@pytest.mark.asyncio
async def test_select_and_dispatch_reports_blocked_in_rejections_not_selected(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worktree-failure BLOCKED item must surface in the tick result
    as a rejection, not a selection, and must NOT burn a slot of the
    within-tick concurrency cap.

    The kernel's ``_select_and_dispatch`` previously counted every call
    to ``_dispatch_item`` as a successful selection. After the M-3
    fail-closed change the dispatch can return early without spawning,
    so the caller now checks ``item.status is AgendaStatus.BLOCKED``
    and routes those into ``rejections``. This test pins that contract
    so a future refactor can't silently re-introduce the miscount.
    """
    store = AgendaStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane({WorkerKind.CLAUDE_CLI: 4}),
    )
    item = _make_operator_gate_item()
    kernel._agenda.add(item, reason="test_seed")

    def _raising_allocate(_record: Any) -> None:
        raise WorktreeError("git missing")

    monkeypatch.setattr(
        kernel_mod, "_allocate_worktree_for_record", _raising_allocate
    )

    async def _fake_rationale(*_a: Any, **_k: Any) -> str:
        return "test rationale"

    monkeypatch.setattr(kernel_mod, "generate_rationale", _fake_rationale)

    selected, rejections = await kernel._select_and_dispatch()

    assert item.id not in selected, (
        "blocked dispatch must not appear in selected"
    )
    blocked_rejections = [r for r in rejections if r["id"] == item.id]
    assert len(blocked_rejections) == 1, (
        f"blocked item must appear once in rejections, got {rejections}"
    )
    assert "worktree_alloc_failed:" in blocked_rejections[0]["reason"]
