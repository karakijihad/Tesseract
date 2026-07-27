"""AU-3 — WorkerLane admission: lane cap + risk-class check."""

from __future__ import annotations

from pathlib import Path

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import (
    AdmissionDecision,
    WorkerLane,
)
from tesseract.orchestrator.workers.record import (
    RiskClass,
    WorkerStatus,
    write_record,
)
from tesseract.tests.fix_pass_autonomy_AU_3.conftest import make_record


def test_admit_when_under_cap_and_risk_within(isolated_home: Path) -> None:
    lane = WorkerLane({WorkerKind.TARS_SELF: 2})
    result = lane.admit(kind=WorkerKind.TARS_SELF, risk_class=RiskClass.AUTONOMOUS)
    assert result.admitted
    assert result.decision == AdmissionDecision.ADMIT
    assert result.running == 0
    assert result.cap == 2


def test_reject_when_lane_full(isolated_home: Path) -> None:
    lane = WorkerLane({WorkerKind.CLAUDE_CLI: 1})
    record = make_record(kind=WorkerKind.CLAUDE_CLI, risk_class=RiskClass.OPERATOR_GATE)
    write_record(record)
    result = lane.admit(kind=WorkerKind.CLAUDE_CLI, risk_class=RiskClass.OPERATOR_GATE)
    assert not result.admitted
    assert result.decision == AdmissionDecision.REJECT_LANE_FULL
    assert result.running == 1
    assert result.cap == 1
    assert result.reason == "lane_full"


def test_reject_when_risk_class_exceeds_ceiling(isolated_home: Path) -> None:
    """An autonomous tars_self lane refuses an operator_gate item —
    the worker kind's ceiling is propose (per default _KIND_CEILING)
    so operator_gate is over it."""
    lane = WorkerLane({WorkerKind.TARS_SELF: 4})
    result = lane.admit(kind=WorkerKind.TARS_SELF, risk_class=RiskClass.OPERATOR_GATE)
    assert not result.admitted
    assert result.decision == AdmissionDecision.REJECT_RISK_MISMATCH
    assert "operator_gate" in result.reason


def test_reject_unconfigured_lane(isolated_home: Path) -> None:
    lane = WorkerLane({WorkerKind.TARS_SELF: 4})
    result = lane.admit(kind=WorkerKind.CLAUDE_CLI, risk_class=RiskClass.OPERATOR_GATE)
    assert not result.admitted
    assert result.decision == AdmissionDecision.REJECT_UNCONFIGURED


def test_from_mission_lanes_block_parses_both_shapes(isolated_home: Path) -> None:
    """Int shape (legacy) AND {max_concurrent: N, retry: {...}} (AU-3 S2)
    both supported. Unknown kinds logged and skipped."""
    lane = WorkerLane.from_mission_lanes_block(
        {
            "tars_self": 4,
            "markdown_agent": {"max_concurrent": 3, "retry": {"max_retries": 1}},
            "openclaw_future_kind": 99,  # unknown — must be skipped
        }
    )
    assert lane.cap_for(WorkerKind.TARS_SELF) == 4
    assert lane.cap_for(WorkerKind.MARKDOWN_AGENT) == 3
    assert lane.cap_for(WorkerKind.CLAUDE_CLI) is None


def test_running_count_only_counts_matching_kind(isolated_home: Path) -> None:
    write_record(make_record(kind=WorkerKind.TARS_SELF, agenda_item_id="ag-a"))
    write_record(make_record(kind=WorkerKind.TARS_SELF, agenda_item_id="ag-b"))
    write_record(make_record(kind=WorkerKind.MARKDOWN_AGENT, agenda_item_id="ag-c"))

    lane = WorkerLane(
        {WorkerKind.TARS_SELF: 4, WorkerKind.MARKDOWN_AGENT: 4}
    )
    assert lane.running_count(WorkerKind.TARS_SELF) == 2
    assert lane.running_count(WorkerKind.MARKDOWN_AGENT) == 1


def test_running_count_filters_terminal_statuses(isolated_home: Path) -> None:
    """Reviewer (AU-3 S1): if a terminal record lingers in active/ before
    the archiver moves it, the lane gauge must NOT count it — otherwise
    a cap=1 lane would falsely reject new work."""
    write_record(make_record(kind=WorkerKind.CLAUDE_CLI, status=WorkerStatus.RUNNING, agenda_item_id="ag-live"))
    write_record(make_record(kind=WorkerKind.CLAUDE_CLI, status=WorkerStatus.DONE, agenda_item_id="ag-doneA"))
    write_record(make_record(kind=WorkerKind.CLAUDE_CLI, status=WorkerStatus.CANCELLED, agenda_item_id="ag-cancelB"))
    write_record(make_record(kind=WorkerKind.CLAUDE_CLI, status=WorkerStatus.FAILED, agenda_item_id="ag-failC"))

    lane = WorkerLane({WorkerKind.CLAUDE_CLI: 1})
    assert lane.running_count(WorkerKind.CLAUDE_CLI) == 1


def test_absolute_deny_never_admitted(isolated_home: Path) -> None:
    lane = WorkerLane({WorkerKind.CLAUDE_CLI: 2})
    result = lane.admit(kind=WorkerKind.CLAUDE_CLI, risk_class=RiskClass.ABSOLUTE_DENY)
    assert not result.admitted
    assert result.decision == AdmissionDecision.REJECT_RISK_MISMATCH
