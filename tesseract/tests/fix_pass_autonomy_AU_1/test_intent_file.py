"""AU-1 — intent.json shape + atomic I/O + staleness checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tesseract.supervisor.intent import (
    IntentFile,
    clear_intent,
    intent_path,
    now_utc,
    read_with_staleness_check,
    write_atomic,
)


def test_intent_round_trip(tmp_path: Path) -> None:
    path = intent_path(tmp_path)
    record = IntentFile(
        intent="operator_quit",
        timestamp=now_utc(),
        source="ui_button",
        reason="operator clicked shutdown",
        backend_pid=12345,
    )
    write_atomic(path, record)
    backend_started = record.timestamp - timedelta(seconds=1)
    read = read_with_staleness_check(
        path, backend_started_at=backend_started, backend_pid=12345,
    )
    assert read is not None
    assert read.intent == "operator_quit"
    assert read.source == "ui_button"
    assert read.backend_pid == 12345


def test_missing_intent_returns_none(tmp_path: Path) -> None:
    path = intent_path(tmp_path)
    assert read_with_staleness_check(
        path, backend_started_at=now_utc(),
    ) is None


def test_malformed_intent_returns_none(tmp_path: Path) -> None:
    path = intent_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    assert read_with_staleness_check(
        path, backend_started_at=now_utc(),
    ) is None


def test_stale_timestamp_returns_none(tmp_path: Path) -> None:
    path = intent_path(tmp_path)
    record = IntentFile(
        intent="operator_quit",
        timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc),
        source="ui_button",
    )
    write_atomic(path, record)
    # backend_started_at is AFTER the intent's timestamp → stale.
    read = read_with_staleness_check(
        path, backend_started_at=now_utc(),
    )
    assert read is None


def test_mismatched_backend_pid_returns_none(tmp_path: Path) -> None:
    path = intent_path(tmp_path)
    record = IntentFile(
        intent="restart_upgrade",
        timestamp=now_utc(),
        source="upgrade_manager",
        continuation_id="ag-X",
        backend_pid=11111,
    )
    write_atomic(path, record)
    backend_started = record.timestamp - timedelta(seconds=1)
    read = read_with_staleness_check(
        path, backend_started_at=backend_started, backend_pid=22222,
    )
    assert read is None


def test_clear_intent_removes_file(tmp_path: Path) -> None:
    path = intent_path(tmp_path)
    record = IntentFile(
        intent="operator_quit", timestamp=now_utc(), source="ui_button",
    )
    write_atomic(path, record)
    assert path.exists()
    clear_intent(path)
    assert not path.exists()
    # Idempotent — second clear on missing file does not raise.
    clear_intent(path)


def test_clear_intent_idempotent_when_missing(tmp_path: Path) -> None:
    clear_intent(intent_path(tmp_path))  # no-op, must not raise


def test_atomic_write_overwrites(tmp_path: Path) -> None:
    path = intent_path(tmp_path)
    first = IntentFile(intent="operator_quit", timestamp=now_utc(), source="ui_button")
    write_atomic(path, first)
    second = IntentFile(intent="restart_upgrade", timestamp=now_utc(), source="upgrade_manager", continuation_id="ag-Y")
    write_atomic(path, second)
    read = read_with_staleness_check(path, backend_started_at=second.timestamp - timedelta(seconds=1))
    assert read is not None
    assert read.intent == "restart_upgrade"
    assert read.continuation_id == "ag-Y"
