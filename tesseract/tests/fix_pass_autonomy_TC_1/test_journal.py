"""TC-1 — JournalWriter primitives + kernel hook integration."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy import (
    AgendaStore,
    AutonomyEvent,
    AutonomyKernel,
    KernelConfig,
    MapperConfig,
    WorkerRunner,
)
from tesseract.orchestrator.autonomy import journal as operator_journal
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.orchestrator.workers.record import (
    ArtifactRef,
    WorkerRecord,
    WorkerStatus,
    write_record,
)


def _read_today(home: Path) -> list[dict]:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = home / "operator_journal" / f"{day}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# ---------- direct writer tests ----------------------------------------


def test_writer_appends_row(isolated_home: Path) -> None:
    operator_journal.append(
        "approval",
        {"agenda_item_id": "ag-doe-1", "summary": "doe approval"},
    )
    rows = _read_today(isolated_home)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "approval"
    assert row["agenda_item_id"] == "ag-doe-1"
    assert row["summary"] == "doe approval"
    assert "ts" in row
    assert row["worker_id"] is None
    assert row["artifacts"] is None
    assert row["follow_up_draft_id"] is None


def test_writer_resolves_path_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching TESSERACT_HOME after a first write must route the
    second write to the new tree — proves no import-time path capture."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("TESSERACT_HOME", str(first))
    operator_journal.append("approval", {"agenda_item_id": "ag-1"})
    monkeypatch.setenv("TESSERACT_HOME", str(second))
    operator_journal.append("approval", {"agenda_item_id": "ag-2"})

    assert _read_today(first)[0]["agenda_item_id"] == "ag-1"
    assert _read_today(second)[0]["agenda_item_id"] == "ag-2"


def test_writer_unknown_event_still_writes(
    isolated_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    operator_journal.append("zzz_bogus", {"agenda_item_id": "ag-doe-2"})
    assert any("unknown event_type" in r.message for r in caplog.records)
    rows = _read_today(isolated_home)
    assert rows[0]["event_type"] == "zzz_bogus"


def test_read_recent_returns_newest_first(isolated_home: Path) -> None:
    for i in range(3):
        operator_journal.append("outcome", {"agenda_item_id": f"ag-{i}"})
    rows = operator_journal.read_recent(limit=10)
    ids = [r["agenda_item_id"] for r in rows]
    assert ids == ["ag-2", "ag-1", "ag-0"]


def test_read_recent_honours_limit(isolated_home: Path) -> None:
    for i in range(5):
        operator_journal.append("outcome", {"agenda_item_id": f"ag-{i}"})
    rows = operator_journal.read_recent(limit=2)
    assert len(rows) == 2
    assert rows[0]["agenda_item_id"] == "ag-4"


def test_read_recent_skips_malformed_lines(isolated_home: Path) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = isolated_home / "operator_journal" / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"event_type": "approval", "agenda_item_id": "ag-1"})
        + "\n{not json}\n"
        + json.dumps({"event_type": "outcome", "agenda_item_id": "ag-2"})
        + "\n",
        encoding="utf-8",
    )
    rows = operator_journal.read_recent(limit=10)
    assert [r["event_type"] for r in rows] == ["outcome", "approval"]


# ---------- kernel hook integration ------------------------------------


class _DoneRunner:
    def __init__(
        self,
        *,
        summary: str = "",
        artifacts: list[ArtifactRef] | None = None,
        status: WorkerStatus = WorkerStatus.DONE,
    ) -> None:
        self.summary = summary
        self.artifacts = artifacts or []
        self.status = status

    async def run(self, record: WorkerRecord) -> None:
        record.summary = self.summary
        record.artifacts = list(self.artifacts)
        record.transition_to(self.status, reason="test_runner")
        write_record(record)


def _make_kernel(
    *,
    runner: WorkerRunner | None = None,
) -> AutonomyKernel:
    lane = WorkerLane(
        {
            WorkerKind.TARS_SELF: 10,
            WorkerKind.MARKDOWN_AGENT: 10,
            WorkerKind.CLAUDE_CLI: 10,
            WorkerKind.CODEX_CLI: 10,
            WorkerKind.TERMINAL: 10,
        }
    )
    mapper_configs = {
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
        config=KernelConfig(top_k=3, max_concurrent_workers_total=8),
        mapper_configs=mapper_configs,
        worker_runner=runner,
    )


@pytest.mark.asyncio
async def test_dispatch_writes_dispatch_row(isolated_home: Path) -> None:
    kernel = _make_kernel(runner=_DoneRunner(summary="doe", artifacts=[]))
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-dispatch"})
    )
    await kernel.tick()
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)

    rows = _read_today(isolated_home)
    dispatched = [r for r in rows if r["event_type"] == "dispatch"]
    assert len(dispatched) == 1
    assert dispatched[0]["worker_id"].startswith("wk-")
    assert dispatched[0]["agenda_item_id"]


@pytest.mark.asyncio
async def test_terminal_done_with_artifacts_writes_outcome_only(
    isolated_home: Path,
) -> None:
    runner = _DoneRunner(
        summary="implemented",
        artifacts=[ArtifactRef(path="out.txt", kind="file")],
    )
    kernel = _make_kernel(runner=runner)
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-done"})
    )
    await kernel.tick()
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)

    rows = _read_today(isolated_home)
    events = [r["event_type"] for r in rows]
    assert "outcome" in events
    assert "advice_only" not in events
    outcome = next(r for r in rows if r["event_type"] == "outcome")
    assert outcome["status"] == "done"
    assert outcome["summary"] == "implemented"
    assert outcome["artifacts"] == 1


@pytest.mark.asyncio
async def test_done_no_artifacts_with_summary_emits_advice_only(
    isolated_home: Path,
) -> None:
    runner = _DoneRunner(summary="here is some advice", artifacts=[])
    kernel = _make_kernel(runner=runner)
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-advice"})
    )
    await kernel.tick()
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)

    rows = _read_today(isolated_home)
    events = [r["event_type"] for r in rows]
    assert "advice_only" in events
    advice = next(r for r in rows if r["event_type"] == "advice_only")
    assert advice["summary"] == "here is some advice"
    assert advice["artifacts"] == 0


@pytest.mark.asyncio
async def test_failed_worker_writes_outcome_no_advice_only(
    isolated_home: Path,
) -> None:
    runner = _DoneRunner(
        summary="partial output",
        artifacts=[],
        status=WorkerStatus.FAILED,
    )
    kernel = _make_kernel(runner=runner)
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-fail"})
    )
    await kernel.tick()
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)

    rows = _read_today(isolated_home)
    events = [r["event_type"] for r in rows]
    assert "outcome" in events
    assert "advice_only" not in events
    outcome = next(r for r in rows if r["event_type"] == "outcome")
    assert outcome["status"] == "failed"


@pytest.mark.asyncio
async def test_done_without_summary_no_advice_only(
    isolated_home: Path,
) -> None:
    runner = _DoneRunner(summary="   ", artifacts=[])
    kernel = _make_kernel(runner=runner)
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-empty-summary"})
    )
    await kernel.tick()
    await asyncio.gather(*list(kernel._dispatch_tasks), return_exceptions=True)

    rows = _read_today(isolated_home)
    events = [r["event_type"] for r in rows]
    assert "outcome" in events
    assert "advice_only" not in events
