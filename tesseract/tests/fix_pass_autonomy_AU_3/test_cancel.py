"""AU-3 — cancellation protocol per kind."""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.cancel import (
    CancelOutcome,
    cancel_worker,
    clear_registry,
    register_canceller,
)
from tesseract.orchestrator.workers.record import (
    WorkerStatus,
    load_record,
    write_record,
)
from tesseract.tests.fix_pass_autonomy_AU_3.conftest import make_record


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    clear_registry()
    yield
    clear_registry()


async def _fake_success(record):  # type: ignore[no-untyped-def]
    return CancelOutcome(cancelled=True, detail="fake_success")


async def _fake_failure(record):  # type: ignore[no-untyped-def]
    return CancelOutcome(cancelled=False, detail="pty_unreachable")


async def _fake_raise(record):  # type: ignore[no-untyped-def]
    raise RuntimeError("canceller_boom")


@pytest.mark.asyncio
async def test_cancel_missing_record_returns_record_missing(isolated_home: Path) -> None:
    outcome = await cancel_worker("wk-never-existed")
    assert not outcome.cancelled
    assert outcome.detail == "record_missing"


@pytest.mark.asyncio
async def test_cancel_already_terminal_returns_already_terminal(isolated_home: Path) -> None:
    record = make_record(status=WorkerStatus.DONE)
    write_record(record)
    outcome = await cancel_worker(record.id)
    assert not outcome.cancelled
    assert outcome.detail.startswith("already_terminal")


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
async def test_cancel_dispatches_per_kind(isolated_home: Path, kind: WorkerKind) -> None:
    """Every kind routes to its registered canceller. Record lands in
    cancelled status, archived after success."""
    register_canceller(kind, _fake_success)
    record = make_record(kind=kind, status=WorkerStatus.RUNNING, agenda_item_id=f"ag-{kind.value}")
    write_record(record)

    outcome = await cancel_worker(record.id, reason="operator_cancelled")
    assert outcome.cancelled
    assert outcome.detail == "fake_success"

    loaded = load_record(record.id)
    assert loaded is not None
    assert loaded.status == WorkerStatus.CANCELLED
    transitions = loaded.status_history
    assert any(t.to_status == "cancelled" and t.reason == "operator_cancelled" for t in transitions)


@pytest.mark.asyncio
async def test_cancel_no_canceller_registered_still_marks_cancelled(isolated_home: Path) -> None:
    """No registered canceller → record IS marked cancelled (operator
    intent recorded) but outcome reports no_canceller so the operator
    knows the underlying process may still be alive."""
    record = make_record(kind=WorkerKind.CLAUDE_CLI, status=WorkerStatus.RUNNING)
    write_record(record)
    outcome = await cancel_worker(record.id)
    assert not outcome.cancelled
    assert outcome.detail == "no_canceller_registered"
    loaded = load_record(record.id)
    assert loaded is not None
    assert loaded.status == WorkerStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_canceller_raises_records_error_class(isolated_home: Path) -> None:
    register_canceller(WorkerKind.TARS_SELF, _fake_raise)
    record = make_record(kind=WorkerKind.TARS_SELF, status=WorkerStatus.RUNNING)
    write_record(record)
    outcome = await cancel_worker(record.id)
    assert not outcome.cancelled
    assert "canceller_raised:RuntimeError" in outcome.detail
    loaded = load_record(record.id)
    assert loaded is not None
    assert loaded.error_class == "RuntimeError"
    assert "canceller_boom" in (loaded.error_message or "")
    assert loaded.status == WorkerStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_failure_detail_recorded_in_error_message(isolated_home: Path) -> None:
    register_canceller(WorkerKind.CLAUDE_CLI, _fake_failure)
    record = make_record(kind=WorkerKind.CLAUDE_CLI, status=WorkerStatus.RUNNING)
    write_record(record)
    outcome = await cancel_worker(record.id)
    assert not outcome.cancelled
    assert outcome.detail == "pty_unreachable"
    loaded = load_record(record.id)
    assert loaded is not None
    assert loaded.error_message == "pty_unreachable"
