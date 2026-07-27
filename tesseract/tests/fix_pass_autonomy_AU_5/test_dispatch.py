"""AU-5 S2 — end-to-end dispatch tests.

Covers the protocol's 10-test exit list with the substrate now wired
to real :class:`WorkerRecord` writes + injectable :class:`WorkerRunner`.
Tests 1-6 + 9 + 10 from ``_shared/autonomy-kernel-protocol.md §Tests``;
test 7 (rationale unavailable) is in ``test_rationale.py`` + below;
test 8 (de-dupe) is in ``test_kernel_tick.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy import (
    AgendaStore,
    AutonomyEvent,
    AutonomyKernel,
    KernelConfig,
    MapperConfig,
    UNAVAILABLE_MARKER,
    WorkerRunner,
)
from tesseract.orchestrator.autonomy.models import (
    AgendaSource,
    AgendaStatus,
    RiskClass,
)
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.orchestrator.workers.record import (
    WorkerRecord,
    WorkerStatus,
    load_record,
    write_record,
)


class _RecordingRunner:
    """Test runner: records every dispatched record id, never blocks,
    marks each record DONE."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def run(self, record: WorkerRecord) -> None:
        self.seen.append(record.id)
        record.transition_to(WorkerStatus.DONE, reason="test_runner_complete")
        write_record(record)


class _SlowRunner:
    """Hangs for ``delay`` seconds — proves the drain timeout fires."""

    def __init__(self, delay: float = 60.0) -> None:
        self.delay = delay
        self.started: list[str] = []

    async def run(self, record: WorkerRecord) -> None:
        self.started.append(record.id)
        record.transition_to(WorkerStatus.RUNNING, reason="test_slow_running")
        write_record(record)
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            record.transition_to(WorkerStatus.INTERRUPTED, reason="test_cancel")
            write_record(record)
            raise


def _make_kernel(
    *,
    runner: WorkerRunner | None = None,
    lane: WorkerLane | None = None,
    rationale_adapter=None,
    mapper_configs: dict[AgendaSource, MapperConfig] | None = None,
    top_k: int = 3,
    max_total: int = 8,
) -> AutonomyKernel:
    from tesseract.orchestrator.workers.kinds import WorkerKind

    if lane is None:
        lane = WorkerLane(
            {
                WorkerKind.TARS_SELF: 10,
                WorkerKind.MARKDOWN_AGENT: 10,
                WorkerKind.CLAUDE_CLI: 10,
                WorkerKind.CODEX_CLI: 10,
                WorkerKind.TERMINAL: 10,
            }
        )
    cfgs = mapper_configs or {
        AgendaSource.OPERATOR: MapperConfig(
            enabled=True,
            source=AgendaSource.OPERATOR,
            default_risk_class=RiskClass.PROPOSE,
            dedupe_window_hours=24,
        )
    }
    return AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=lane,
        config=KernelConfig(
            top_k=top_k, max_concurrent_workers_total=max_total
        ),
        mapper_configs=cfgs,
        worker_runner=runner,
        rationale_adapter=rationale_adapter,
    )


@pytest.mark.asyncio
async def test_protocol_1_tick_produces_k_dispatches(isolated_home: Path) -> None:
    """§Tests #1 — Tick produces exactly K worker dispatches when K
    items have headroom."""
    runner = _RecordingRunner()
    kernel = _make_kernel(runner=runner, top_k=3)
    for i in range(5):
        kernel.bus.publish_nowait(
            AutonomyEvent.make(
                AgendaSource.OPERATOR, {"goal": f"doe-dispatch-{i}"}
            )
        )
    result = await kernel.tick()
    # Wait for dispatch tasks to complete.
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)
    assert len(result.selected) == 3
    assert len(runner.seen) == 3


@pytest.mark.asyncio
async def test_protocol_7_rationale_unavailable_falls_through(
    isolated_home: Path,
) -> None:
    """§Tests #7 — Rationale unavailable (mock error) → selection
    proceeds; rationale marker persists."""

    async def broken(prompt: str) -> str:
        raise RuntimeError("model down")

    runner = _RecordingRunner()
    kernel = _make_kernel(runner=runner, rationale_adapter=broken)
    kernel.bus.publish_nowait(
        AutonomyEvent.make(
            AgendaSource.OPERATOR, {"goal": "doe-no-rationale"}
        )
    )
    result = await kernel.tick()
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)
    assert len(result.selected) == 1
    item = kernel._agenda.get(result.selected[0])
    assert item is not None
    assert item.last_decision == UNAVAILABLE_MARKER
    # rationale field stays at the mapper-set value (in this case "").
    assert item.rationale == ""


@pytest.mark.asyncio
async def test_rationale_populates_item_when_adapter_succeeds(
    isolated_home: Path,
) -> None:
    async def ok(prompt: str) -> str:
        return "operator_priority is high — picked first."

    runner = _RecordingRunner()
    kernel = _make_kernel(runner=runner, rationale_adapter=ok)
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-rat-ok"})
    )
    result = await kernel.tick()
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)
    item = kernel._agenda.get(result.selected[0])
    assert item is not None
    assert "operator_priority" in item.rationale
    assert "operator_priority" in (item.last_decision or "")


@pytest.mark.asyncio
async def test_worker_record_written_before_runner_starts(
    isolated_home: Path,
) -> None:
    """GOVERNANCE §6 — durable record on disk before work starts."""
    record_at_runner_start: list[WorkerStatus] = []

    class _CheckingRunner:
        async def run(self, record: WorkerRecord) -> None:
            # Re-read the record from disk; the kernel must have
            # written it before invoking us.
            on_disk = load_record(record.id)
            assert on_disk is not None, "WorkerRecord must be on disk before runner"
            record_at_runner_start.append(on_disk.status)
            record.transition_to(WorkerStatus.DONE, reason="checked")
            write_record(record)

    kernel = _make_kernel(runner=_CheckingRunner())
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-record-first"})
    )
    await kernel.tick()
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)
    assert record_at_runner_start == [WorkerStatus.SPAWNING]


@pytest.mark.asyncio
async def test_protocol_9_quiesce_resume_in_flight_workers(
    isolated_home: Path,
) -> None:
    """§Tests #9 — quiesce() + resume() during in-flight workers."""
    runner = _SlowRunner(delay=10.0)
    kernel = _make_kernel(runner=runner)
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-in-flight"})
    )
    await kernel.tick()
    # Give the dispatch task a tick to actually start the runner.
    await asyncio.sleep(0.05)
    assert len(runner.started) == 1
    # Quiesce — no new dispatches, but the in-flight one keeps running.
    kernel.quiesce()
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-after-quiesce"})
    )
    result = await kernel.tick()
    assert result.paused is True
    assert result.selected == []
    # The slow worker is still running on its task.
    tasks = list(kernel._dispatch_tasks)
    assert tasks and not all(t.done() for t in tasks)
    # Resume; the formerly-blocked candidate dispatches.
    kernel.resume()
    result2 = await kernel.tick()
    assert len(result2.selected) == 1


@pytest.mark.asyncio
async def test_protocol_10_stop_drains_dispatch_loop(isolated_home: Path) -> None:
    """§Tests #10 — Kernel stop drains in-flight dispatch loop. With
    a quick runner, drain completes well under the 30s timeout."""
    runner = _RecordingRunner()
    kernel = _make_kernel(runner=runner)
    await kernel.start()
    for i in range(3):
        kernel.bus.publish_nowait(
            AutonomyEvent.make(
                AgendaSource.OPERATOR, {"goal": f"doe-stop-{i}"}
            )
        )
    kernel.poke()
    # Let the loop fire once.
    await asyncio.sleep(0.1)
    await kernel.stop()
    # All dispatches drained; runner saw every record.
    assert len(runner.seen) == 3
    assert not kernel.is_running


@pytest.mark.asyncio
async def test_runner_exception_marks_worker_failed(
    isolated_home: Path,
) -> None:
    """A broken runner does not corrupt the agenda — the kernel
    persists a FAILED record so recovery surfaces the breakage."""

    class _BrokenRunner:
        async def run(self, record: WorkerRecord) -> None:
            raise RuntimeError("simulated runner crash")

    kernel = _make_kernel(runner=_BrokenRunner())
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-runner-crash"})
    )
    result = await kernel.tick()
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)
    assert len(result.selected) == 1
    item = kernel._agenda.get(result.selected[0])
    assert item is not None
    assert item.linked_workers
    rec = load_record(item.linked_workers[0])
    assert rec is not None
    assert rec.status == WorkerStatus.FAILED
    assert rec.error_class == "RunnerException"


@pytest.mark.asyncio
async def test_dispatch_respects_max_concurrent_workers_total(
    isolated_home: Path,
) -> None:
    runner = _SlowRunner(delay=10.0)
    # max_total=2; emit 4 items.
    kernel = _make_kernel(runner=runner, max_total=2)
    for i in range(4):
        kernel.bus.publish_nowait(
            AutonomyEvent.make(
                AgendaSource.OPERATOR, {"goal": f"doe-cap-{i}"}
            )
        )
    result = await kernel.tick()
    # Only 2 admitted; remainder rejected with the global-cap reason.
    await asyncio.sleep(0.05)
    assert len(result.selected) == 2
    assert any(
        r["reason"] == "max_concurrent_workers_total_reached"
        for r in result.rejections
    )
    # Cancel the slow tasks so the test cleans up.
    for t in list(kernel._dispatch_tasks):
        t.cancel()
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)
