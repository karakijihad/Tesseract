"""X-4 Session A — `events_log.append_event` / `read_events_since`
contract: byte-offset cursor, idempotent reads, monotonic advancement,
graceful skip on a malformed line."""

from __future__ import annotations

import json
from pathlib import Path

from tesseract.orchestrator.tars_controller.lanes import (
    LaneEvent,
    LaneEventsCursor,
    append_event,
    read_events_since,
)


def _evt(lane_id: str, kind: str, text: str) -> LaneEvent:
    return LaneEvent(lane_id=lane_id, kind=kind, payload={"text": text})


def test_append_then_read_from_zero(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    append_event(log, _evt("lane-1", "assistant_text", "hello"))
    append_event(log, _evt("lane-1", "turn_ended", "done"))
    events, next_cursor = read_events_since(log, LaneEventsCursor(0))
    assert [e.kind for e in events] == ["assistant_text", "turn_ended"]
    assert next_cursor.byte_offset == log.stat().st_size


def test_read_is_idempotent_on_same_cursor(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    append_event(log, _evt("lane-1", "assistant_text", "one"))
    append_event(log, _evt("lane-1", "assistant_text", "two"))
    events_a, cursor_a = read_events_since(log, LaneEventsCursor(0))
    events_b, cursor_b = read_events_since(log, LaneEventsCursor(0))
    assert [e.payload["text"] for e in events_a] == ["one", "two"]
    assert [e.payload["text"] for e in events_b] == ["one", "two"]
    assert cursor_a == cursor_b


def test_cursor_advances_past_seen_events(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    append_event(log, _evt("lane-1", "assistant_text", "first"))
    first, cursor = read_events_since(log, LaneEventsCursor(0))
    append_event(log, _evt("lane-1", "assistant_text", "second"))
    second, _ = read_events_since(log, cursor)
    assert [e.payload["text"] for e in first] == ["first"]
    assert [e.payload["text"] for e in second] == ["second"]


def test_cursor_stamps_line_start_offset(tmp_path: Path) -> None:
    """Each returned event's `cursor` field is the byte offset of its
    own line — a future caller passing that cursor would skip past it."""
    log = tmp_path / "events.jsonl"
    append_event(log, _evt("lane-1", "assistant_text", "a"))
    append_event(log, _evt("lane-1", "assistant_text", "b"))
    events, _ = read_events_since(log, LaneEventsCursor(0))
    assert events[0].cursor == "0"
    # Second line starts where the first line ended.
    line_one_len = len(log.read_text(encoding="utf-8").splitlines(keepends=True)[0])
    assert events[1].cursor == str(line_one_len)


def test_malformed_line_does_not_wedge_reader(tmp_path: Path) -> None:
    """A garbage line MUST be skipped so the next valid line still
    surfaces and the cursor still advances past EOF."""
    log = tmp_path / "events.jsonl"
    append_event(log, _evt("lane-1", "assistant_text", "good-1"))
    # Inject one bad line manually.
    with log.open("ab") as fh:
        fh.write(b"{not valid json\n")
    append_event(log, _evt("lane-1", "assistant_text", "good-2"))

    events, next_cursor = read_events_since(log, LaneEventsCursor(0))
    texts = [e.payload["text"] for e in events]
    assert texts == ["good-1", "good-2"]
    assert next_cursor.byte_offset == log.stat().st_size


def test_missing_log_file_returns_empty(tmp_path: Path) -> None:
    """Before the first append the file does not exist; reader must
    handle the case without raising."""
    events, cursor = read_events_since(
        tmp_path / "never-written.jsonl", LaneEventsCursor(0)
    )
    assert events == []
    assert cursor.byte_offset == 0


def test_cursor_parse_round_trip() -> None:
    cursor = LaneEventsCursor.parse("123")
    assert cursor.byte_offset == 123
    assert cursor.wire == "123"
    assert LaneEventsCursor.parse(None).byte_offset == 0
    assert LaneEventsCursor.parse("").byte_offset == 0


def test_events_jsonl_is_one_object_per_line(tmp_path: Path) -> None:
    """Wire-format invariant: each line parses as exactly one JSON object."""
    log = tmp_path / "events.jsonl"
    append_event(log, _evt("lane-1", "assistant_text", "a"))
    append_event(log, _evt("lane-1", "assistant_text", "b"))
    raw = log.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 2
    for line in raw:
        parsed = json.loads(line)
        assert isinstance(parsed, dict)
        assert parsed["kind"] in {"assistant_text"}
