"""AU-3 S2 — per-kind WorkerRecovery handlers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.recovery.transitions import (
    REASON_PANE_LOST,
    REASON_STALE_HEARTBEAT,
    REASON_WORKER_LOST,
)
from tesseract.orchestrator.workers.heartbeat import touch_heartbeat
from tesseract.orchestrator.workers.record import (
    WorkerStatus,
    load_record,
    write_record,
)
from tesseract.orchestrator.workers.recovery import (
    classify_recovery_reason,
    is_pid_alive,
    recover_worker,
    recover_worker_sync,
    register_recovery_handler,
    reset_recovery_handlers,
)
from tesseract.tests.fix_pass_autonomy_AU_3.conftest import make_record


@pytest.fixture(autouse=True)
def reset_handlers() -> None:
    reset_recovery_handlers()
    yield
    reset_recovery_handlers()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        WorkerKind.TARS_SELF,
        WorkerKind.MARKDOWN_AGENT,
        WorkerKind.CLAUDE_CLI,
        WorkerKind.CODEX_CLI,
        WorkerKind.TERMINAL,
    ],
)
async def test_default_handler_marks_interrupted(isolated_home: Path, kind: WorkerKind) -> None:
    """Every kind's default handler is the conservative interrupt-only
    variant in AU-3 S2. AU-5 will register richer resume handlers."""
    record = make_record(kind=kind, status=WorkerStatus.RUNNING, agenda_item_id=f"ag-{kind.value}")
    write_record(record)

    result = await recover_worker(record.id)
    assert result is not None
    assert result.status == WorkerStatus.INTERRUPTED

    loaded = load_record(record.id)
    assert loaded is not None
    assert loaded.status == WorkerStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_terminal_record_passes_through(isolated_home: Path) -> None:
    record = make_record(status=WorkerStatus.DONE)
    write_record(record)
    result = await recover_worker(record.id)
    assert result is not None
    assert result.status == WorkerStatus.DONE


@pytest.mark.asyncio
async def test_missing_record_returns_none(isolated_home: Path) -> None:
    result = await recover_worker("wk-never-existed")
    assert result is None


def test_classify_reason_stale_heartbeat(isolated_home: Path) -> None:
    """Stale heartbeat takes precedence over PTY-bound classification."""
    record = make_record(kind=WorkerKind.CLAUDE_CLI, status=WorkerStatus.RUNNING)
    write_record(record)
    # Touch heartbeat far in the past (well over 90s).
    touch_heartbeat(record.id, now=1.0)
    assert classify_recovery_reason(record) == REASON_STALE_HEARTBEAT


def test_classify_reason_pane_lost_for_pty_kinds(isolated_home: Path) -> None:
    """No heartbeat at all + PTY kind → pane_lost (not stale_heartbeat —
    the file doesn't exist so `is_heartbeat_stale` says stale, but the
    actual ordering is stale first then PTY)."""
    # When heartbeat is fresh, stale check returns False, and PTY kinds
    # fall through to PANE_LOST. Touch heartbeat now.
    record = make_record(kind=WorkerKind.CLAUDE_CLI, status=WorkerStatus.RUNNING)
    write_record(record)
    touch_heartbeat(record.id)
    assert classify_recovery_reason(record) == REASON_PANE_LOST


def test_classify_reason_worker_lost_for_non_pty_kinds(isolated_home: Path) -> None:
    record = make_record(kind=WorkerKind.TARS_SELF, status=WorkerStatus.RUNNING)
    write_record(record)
    touch_heartbeat(record.id)
    assert classify_recovery_reason(record) == REASON_WORKER_LOST


@pytest.mark.asyncio
async def test_custom_resume_handler_can_recover(isolated_home: Path) -> None:
    """AU-5 will register richer handlers; ensure the protocol composes."""

    class _ResumeFake:
        def can_recover(self, record):  # type: ignore[no-untyped-def]
            return True

        async def resume(self, record):  # type: ignore[no-untyped-def]
            # Transition through SPAWNING so the history captures the
            # resume hop even when the prior status was already RUNNING.
            record.transition_to(WorkerStatus.SPAWNING, reason="resumed_by_handler")
            record.transition_to(WorkerStatus.RUNNING, reason="resumed_back_to_running")
            write_record(record)
            return record

        async def mark_interrupted(self, record, reason):  # type: ignore[no-untyped-def]
            record.transition_to(WorkerStatus.INTERRUPTED, reason=reason)
            write_record(record)
            return record

    register_recovery_handler(WorkerKind.TARS_SELF, _ResumeFake())
    record = make_record(kind=WorkerKind.TARS_SELF, status=WorkerStatus.RUNNING)
    write_record(record)

    result = await recover_worker(record.id)
    assert result is not None
    assert result.status == WorkerStatus.RUNNING
    assert any("resumed_by_handler" in t.reason for t in result.status_history)


@pytest.mark.asyncio
async def test_resume_handler_failure_falls_back_to_interrupted(isolated_home: Path) -> None:
    class _RaisingResume:
        def can_recover(self, record):  # type: ignore[no-untyped-def]
            return True

        async def resume(self, record):  # type: ignore[no-untyped-def]
            raise RuntimeError("resume_broken")

        async def mark_interrupted(self, record, reason):  # type: ignore[no-untyped-def]
            record.transition_to(WorkerStatus.INTERRUPTED, reason=reason)
            write_record(record)
            return record

    register_recovery_handler(WorkerKind.TARS_SELF, _RaisingResume())
    record = make_record(kind=WorkerKind.TARS_SELF, status=WorkerStatus.RUNNING)
    write_record(record)

    result = await recover_worker(record.id)
    assert result is not None
    assert result.status == WorkerStatus.INTERRUPTED


def test_recover_worker_sync_drives_async_handler(isolated_home: Path) -> None:
    record = make_record(kind=WorkerKind.MARKDOWN_AGENT, status=WorkerStatus.RUNNING)
    write_record(record)
    result = recover_worker_sync(record.id)
    assert result is not None
    assert result.status == WorkerStatus.INTERRUPTED


def test_is_pid_alive_current_process(isolated_home: Path) -> None:
    """The current process is always alive — sanity check the probe."""
    assert is_pid_alive(os.getpid())


def test_is_pid_alive_none_or_invalid(isolated_home: Path) -> None:
    assert not is_pid_alive(None)
    assert not is_pid_alive(0)
    assert not is_pid_alive(-1)


def test_is_pid_alive_known_dead_pid(isolated_home: Path) -> None:
    """PID 2**31 - 2 is essentially never alive on a typical system —
    if it ever is, that's a sign the probe semantic needs rethinking."""
    very_high_pid = 2**31 - 2
    assert not is_pid_alive(very_high_pid)
