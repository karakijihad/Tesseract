"""File-log writers must emit local-zone ISO with offset, not raw UTC.

Operator directive: when opening `runs.jsonl`, session JSON, circuit-breaker
logs, or approval-log in a text editor, the timestamps should match the
machine's local clock. Envelopes on the WS stay UTC for sort/cache stability
— only on-disk human-read surfaces are local.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tesseract.brain.session_store import _now_iso
from tesseract.context.circuit_breaker import CircuitBreaker
from tesseract.scheduler.log import append_run_log
from tesseract.scheduler.types import JobContext, JobResult


def _local_offset_seconds() -> int | None:
    """Local zone's current UTC offset in seconds, or None on UTC systems."""
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds()) if offset is not None else None


def _parse_offset(iso: str) -> int | None:
    dt = datetime.fromisoformat(iso)
    return int(dt.utcoffset().total_seconds()) if dt.utcoffset() is not None else None


def test_session_store_now_iso_is_local_zone() -> None:
    ts = _now_iso()
    dt = datetime.fromisoformat(ts)
    assert dt.tzinfo is not None, "session_store._now_iso must emit tz-aware ISO"
    assert _parse_offset(ts) == _local_offset_seconds(), (
        f"session_store._now_iso should match local-zone offset; got {ts!r}"
    )


def test_runs_jsonl_timestamps_are_local_zone(tmp_path: Path) -> None:
    ctx = JobContext(job_name="unit_test_job", fired_at=datetime.now(timezone.utc))
    result = JobResult(
        job_name="unit_test_job",
        run_id="run_abc",
        ok=True,
        detail="ok",
        payload={},
        duration_ms=5,
    )
    append_run_log(ctx, result, log_dir=tmp_path)

    line = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip()
    entry = json.loads(line)

    fired_offset = _parse_offset(entry["fired_at"])
    completed_offset = _parse_offset(entry["completed_at"])
    local_offset = _local_offset_seconds()
    assert fired_offset == local_offset, (
        f"runs.jsonl fired_at must be local-zone; got {entry['fired_at']!r} "
        f"(offset={fired_offset}, expected={local_offset})"
    )
    assert completed_offset == local_offset, (
        f"runs.jsonl completed_at must be local-zone; got {entry['completed_at']!r}"
    )


def test_circuit_breaker_log_is_local_zone(tmp_path: Path) -> None:
    cb = CircuitBreaker(name="unit_test", max_failures=1, log_dir=tmp_path)
    cb.record_failure("boom")  # trips immediately at max_failures=1

    line = (tmp_path / "unit_test.jsonl").read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert _parse_offset(entry["timestamp"]) == _local_offset_seconds(), (
        f"circuit-breaker log timestamp must be local-zone; got {entry['timestamp']!r}"
    )


