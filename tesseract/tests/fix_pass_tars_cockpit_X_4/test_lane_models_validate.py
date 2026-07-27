"""X-4 Session A — Pydantic round-trip for Lane / LaneEvent / LaneStatus
/ LaneSnapshot / LaneSendResult per `_shared/lane-contract.md` v1.

Pins the contract at the wire. Distinct LaneEventKind values for
`assistant_text` and `tool_result` are enforced — the audit-2026-05-24
Critical regression guard."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tesseract.orchestrator.tars_controller.lanes import (
    Lane,
    LaneEvent,
    LaneSendResult,
    LaneSnapshot,
    LaneStatus,
)


def test_lane_round_trip() -> None:
    lane = Lane(
        lane_id="lane-claude-deadbeef0001",
        kind="claude",
        mode="headless",
        model="claude-sonnet-4-6",
        working_dir="/tmp/proj",
        env={"FOO": "BAR"},
    )
    raw = lane.model_dump_json()
    parsed = Lane.model_validate_json(raw)
    assert parsed == lane
    assert parsed.lifecycle == "spawning"
    assert parsed.cli_session_id is None


def test_lane_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        Lane(
            lane_id="x",
            kind="gemini",  # type: ignore[arg-type]
            mode="headless",
            model="m",
            working_dir="/tmp",
        )


def test_lane_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        Lane(
            lane_id="x",
            kind="claude",
            mode="tmux",  # type: ignore[arg-type]
            model="m",
            working_dir="/tmp",
        )


def test_lane_event_assistant_text_and_tool_result_are_distinct_kinds() -> None:
    """Audit-2026-05-24 Critical regression guard. The two kinds MUST
    parse independently and round-trip without conflation."""
    assistant = LaneEvent(
        lane_id="lane-x",
        kind="assistant_text",
        payload={"text": "hello"},
    )
    tool = LaneEvent(
        lane_id="lane-x",
        kind="tool_result",
        payload={"tool_use_id": "tu-1", "output": "ok", "is_error": False},
    )
    assert assistant.kind == "assistant_text"
    assert tool.kind == "tool_result"
    # Round-trip preserves the discrimination.
    a2 = LaneEvent.model_validate_json(assistant.model_dump_json())
    t2 = LaneEvent.model_validate_json(tool.model_dump_json())
    assert a2.kind != t2.kind


def test_lane_status_minimal() -> None:
    status = LaneStatus(alive=True, busy=False)
    assert status.queue_depth == 0
    assert status.lifecycle == "spawning"


def test_lane_send_result_with_reason() -> None:
    r = LaneSendResult(accepted=False, queue_depth=0, reason="lane is closed")
    assert r.accepted is False
    assert r.reason == "lane is closed"


def test_lane_snapshot_contains_recent_events_and_cursor() -> None:
    lane = Lane(
        lane_id="lane-codex-cafebabe0001",
        kind="codex",
        mode="headless",
        model="gpt-5",
        working_dir="/tmp/proj",
    )
    snap = LaneSnapshot(
        lane=lane,
        status=LaneStatus(alive=True, busy=False),
        recent_events=[
            LaneEvent(lane_id=lane.lane_id, kind="turn_started", payload={"turn_id": "t1"}),
            LaneEvent(lane_id=lane.lane_id, kind="assistant_text", payload={"text": "hi"}),
        ],
        next_cursor="128",
    )
    assert len(snap.recent_events) == 2
    assert snap.next_cursor == "128"
