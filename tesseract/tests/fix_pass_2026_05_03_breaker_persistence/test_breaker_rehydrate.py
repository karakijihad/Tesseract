"""CircuitBreaker must rehydrate `is_tripped` from its persisted JSONL.

Without this, a fresh process loses the tripped state, so the next
successful call short-circuits at `if self.is_tripped:` in
`record_success` and never appends a "reset" event — leaving the JSONL
(and the mirror UI that reads it) stuck open forever.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.context.circuit_breaker import CircuitBreaker


def _write_events(log_dir: Path, name: str, events: list[dict]) -> Path:
    path = log_dir / f"{name}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return path


def test_no_log_file_starts_closed(tmp_path: Path) -> None:
    cb = CircuitBreaker(name="probe", max_failures=3, log_dir=tmp_path)
    assert cb.is_tripped is False
    assert cb.failure_count == 0


def test_last_event_tripped_rehydrates_open(tmp_path: Path) -> None:
    _write_events(tmp_path, "probe", [
        {"event": "tripped", "breaker": "probe", "failures": 3, "error": "x", "timestamp": "2026-05-01T00:00:00+00:00"},
    ])
    cb = CircuitBreaker(name="probe", max_failures=3, log_dir=tmp_path)
    assert cb.is_tripped is True
    assert cb.failure_count == 3


def test_last_event_reset_rehydrates_closed(tmp_path: Path) -> None:
    _write_events(tmp_path, "probe", [
        {"event": "tripped", "breaker": "probe", "failures": 3, "error": "x", "timestamp": "2026-05-01T00:00:00+00:00"},
        {"event": "reset", "breaker": "probe", "timestamp": "2026-05-02T00:00:00+00:00"},
    ])
    cb = CircuitBreaker(name="probe", max_failures=3, log_dir=tmp_path)
    assert cb.is_tripped is False
    assert cb.failure_count == 0


def test_record_success_after_rehydrate_writes_reset_event(tmp_path: Path) -> None:
    log_path = _write_events(tmp_path, "probe", [
        {"event": "tripped", "breaker": "probe", "failures": 3, "error": "x", "timestamp": "2026-05-01T00:00:00+00:00"},
    ])

    cb = CircuitBreaker(name="probe", max_failures=3, log_dir=tmp_path)
    assert cb.is_tripped is True

    cb.record_success()

    assert cb.is_tripped is False
    assert cb.failure_count == 0

    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines[-1]["event"] == "reset"
    assert lines[-1]["breaker"] == "probe"


def test_corrupted_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    path.write_text(
        '{"event": "tripped", "breaker": "probe", "failures": 3, "timestamp": "2026-05-01T00:00:00+00:00"}\n'
        'NOT JSON\n',
        encoding="utf-8",
    )
    cb = CircuitBreaker(name="probe", max_failures=3, log_dir=tmp_path)
    assert cb.is_tripped is True


def test_no_log_dir_skips_rehydrate(tmp_path: Path) -> None:
    cb = CircuitBreaker(name="probe", max_failures=3, log_dir=None)
    assert cb.is_tripped is False
    assert cb.failure_count == 0
