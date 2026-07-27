"""AU-3 — heartbeat write + staleness check."""

from __future__ import annotations

import time
from pathlib import Path

from tesseract.orchestrator.workers.heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    STALENESS_THRESHOLD_SECONDS,
    heartbeat_path,
    is_heartbeat_stale,
    read_heartbeat_mtime,
    touch_heartbeat,
)
from tesseract.orchestrator.workers.record import write_record
from tesseract.tests.fix_pass_autonomy_AU_3.conftest import make_record


def test_constants_match_schema(isolated_home: Path) -> None:
    """Schema-locked 30s / 90s pair. Tuning belongs in YAML, not code."""
    assert HEARTBEAT_INTERVAL_SECONDS == 30.0
    assert STALENESS_THRESHOLD_SECONDS == 90.0


def test_touch_creates_heartbeat_file(isolated_home: Path) -> None:
    record = make_record()
    write_record(record)
    path = touch_heartbeat(record.id)
    assert path.exists()
    assert path == heartbeat_path(record.id)


def test_touch_updates_mtime(isolated_home: Path) -> None:
    record = make_record()
    write_record(record)
    touch_heartbeat(record.id, now=1_000_000.0)
    assert read_heartbeat_mtime(record.id) == 1_000_000.0
    touch_heartbeat(record.id, now=1_000_500.0)
    assert read_heartbeat_mtime(record.id) == 1_000_500.0


def test_is_heartbeat_stale_when_old(isolated_home: Path) -> None:
    record = make_record()
    write_record(record)
    touch_heartbeat(record.id, now=1_000_000.0)
    assert not is_heartbeat_stale(record.id, now=1_000_080.0)  # 80s < 90s
    assert is_heartbeat_stale(record.id, now=1_000_100.0)      # 100s > 90s


def test_is_heartbeat_stale_when_missing(isolated_home: Path) -> None:
    """No heartbeat file → treated as stale. Same semantic as a too-old
    file: recovery cannot tell live from dead, fails safe to interrupted."""
    assert is_heartbeat_stale("wk-never-existed")


def test_touch_without_worker_dir_creates_parent(isolated_home: Path) -> None:
    """The heartbeat helper is independent of write_record — it creates
    the parent dir on first touch. (In practice write_record runs first
    but the test confirms the contract.)"""
    path = touch_heartbeat("wk-2026-05-17-1200-tars_self-abc123")
    assert path.exists()
    assert path.parent.is_dir()


def test_read_mtime_returns_none_when_absent(isolated_home: Path) -> None:
    assert read_heartbeat_mtime("wk-ghost") is None
