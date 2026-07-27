"""AU-1 S2 — crash-storm circuit breaker.

Covers kill-switch §Tests #5 (latches) and #6 (--force bypass clears).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tesseract.supervisor.breaker import (
    CRASH_THRESHOLD,
    CRASH_WINDOW_SECONDS,
    CrashStormBreaker,
    crash_storm_archive_dir,
    crash_storm_path,
)


def test_breaker_latches_at_threshold(tmp_path: Path) -> None:
    """Three crashes within the window → marker on disk, return True."""
    breaker = CrashStormBreaker(tesseract_home=tmp_path)
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert breaker.record_crash(exit_code=11, now=base) is False
    assert breaker.record_crash(exit_code=11, now=base + timedelta(seconds=30)) is False
    latched = breaker.record_crash(exit_code=11, now=base + timedelta(seconds=60))
    assert latched is True
    assert breaker.is_latched()
    payload = json.loads(crash_storm_path(tmp_path).read_text(encoding="utf-8"))
    assert len(payload["crashes"]) == CRASH_THRESHOLD
    assert "supervisor halted" in payload["reason"]


def test_breaker_window_eviction(tmp_path: Path) -> None:
    """Old crashes outside the window don't accumulate toward the latch."""
    breaker = CrashStormBreaker(tesseract_home=tmp_path)
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    # First two crashes far in the past — should evict.
    breaker.record_crash(exit_code=11, now=base)
    breaker.record_crash(exit_code=11, now=base + timedelta(seconds=30))
    # Third crash beyond window from the first two → only one survives.
    assert breaker.record_crash(
        exit_code=11, now=base + timedelta(seconds=CRASH_WINDOW_SECONDS + 60),
    ) is False
    assert not breaker.is_latched()


def test_clear_archives_marker(tmp_path: Path) -> None:
    """Clear moves the marker into the archive dir and removes the live file."""
    breaker = CrashStormBreaker(tesseract_home=tmp_path)
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(CRASH_THRESHOLD):
        breaker.record_crash(exit_code=11, now=base + timedelta(seconds=i * 10))
    assert breaker.is_latched()

    archived = breaker.clear()
    assert archived is not None
    assert archived.exists()
    assert archived.parent == crash_storm_archive_dir(tmp_path)
    assert not crash_storm_path(tmp_path).exists()
    # Idempotent — second clear with no marker returns None.
    assert breaker.clear() is None


def test_clear_no_collision_on_same_second(tmp_path: Path) -> None:
    """Two clears within the same second don't overwrite each other."""
    breaker = CrashStormBreaker(tesseract_home=tmp_path)
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    # Latch + clear twice in quick succession.
    archives = []
    for _ in range(2):
        for i in range(CRASH_THRESHOLD):
            breaker.record_crash(exit_code=11, now=base + timedelta(seconds=i * 10))
        a = breaker.clear()
        assert a is not None
        archives.append(a)
    assert archives[0] != archives[1]


def test_force_flag_clears_marker(tmp_path: Path) -> None:
    """`python -m tesseract.supervisor --force` archives the marker and
    continues. Simulated by calling the same path the entry-point uses.
    """
    breaker = CrashStormBreaker(tesseract_home=tmp_path)
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(CRASH_THRESHOLD):
        breaker.record_crash(exit_code=11, now=base + timedelta(seconds=i * 10))
    assert breaker.is_latched()
    # __main__.py calls breaker.clear() when --force is passed; verify
    # the behavior matches: marker gone, archive present.
    archived = breaker.clear()
    assert archived is not None
    assert not breaker.is_latched()
