"""Phase 1 (CLI parity) — sessions archive + per-day grouping.

Tests `archive_old_sessions`, `list_sessions_by_day`, `list_archive`
on a temp-dir fixture so production sessions/ is never touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.brain.session_store import (
    ARCHIVE_AGE_DAYS,
    archive_old_sessions,
    list_archive,
    list_sessions_by_day,
)


def _write_session(
    session_dir: Path, name: str, *, started: str, ended: str | None = None,
    turns: int = 1, model: str = "stub-model",
) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{name}.json"
    payload = {
        "schema": 1,
        "started_at": started,
        "ended_at": ended or started,
        "turn_count": turns,
        "model": model,
        "history": [
            {"role": "user", "content": "hi", "timestamp": started},
            {"role": "assistant", "content": "hello", "timestamp": started},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_archive_moves_only_files_older_than_threshold(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    today = datetime(2026, 5, 10, tzinfo=timezone.utc)

    # 8 days old → archived
    old = _write_session(
        sessions, "2026-05-02-1430",
        started=(today - timedelta(days=8)).isoformat(),
    )
    # 3 days old → kept
    recent = _write_session(
        sessions, "2026-05-07-1100",
        started=(today - timedelta(days=3)).isoformat(),
    )
    # Custom-named, no date prefix → never archived
    custom = _write_session(
        sessions, "before-rebase",
        started=(today - timedelta(days=30)).isoformat(),
    )

    moved = archive_old_sessions(sessions, days=7, now=today)

    assert len(moved) == 1
    assert not old.exists()
    assert recent.exists()
    assert custom.exists()
    archived = sessions / "archive" / "2026-05" / "2026-05-02-1430.json"
    assert archived.exists()


def test_archive_idempotent_second_run_moves_nothing(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    today = datetime(2026, 5, 10, tzinfo=timezone.utc)
    _write_session(
        sessions, "2026-04-20-0900",
        started=(today - timedelta(days=20)).isoformat(),
    )

    first = archive_old_sessions(sessions, days=7, now=today)
    second = archive_old_sessions(sessions, days=7, now=today)

    assert len(first) == 1
    assert second == []


def test_archive_respects_default_age_constant() -> None:
    assert ARCHIVE_AGE_DAYS == 7


def test_list_sessions_by_day_groups_runs(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _write_session(sessions, "2026-05-10-0900", started="2026-05-10T09:00:00+00:00", turns=3)
    _write_session(sessions, "2026-05-10-1430", started="2026-05-10T14:30:00+00:00", turns=5)
    _write_session(sessions, "2026-05-09-2200", started="2026-05-09T22:00:00+00:00", turns=2)
    _write_session(sessions, "before-rebase", started="2026-05-08T10:00:00+00:00", turns=1)

    days = list_sessions_by_day(sessions)

    # Newest day first; "custom" bucket sorts last
    assert [d["date"] for d in days] == ["2026-05-10", "2026-05-09", "custom"]
    may_10 = days[0]
    assert may_10["run_count"] == 2
    assert may_10["total_turns"] == 8
    # Within a day, runs are newest-started first
    assert [r["session_id"] for r in may_10["runs"]] == [
        "2026-05-10-1430",
        "2026-05-10-0900",
    ]


def test_list_sessions_by_day_excludes_archive(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    today = datetime(2026, 5, 10, tzinfo=timezone.utc)
    _write_session(sessions, "2026-05-09-0900", started="2026-05-09T09:00:00+00:00")
    _write_session(
        sessions, "2026-04-20-0900",
        started=(today - timedelta(days=20)).isoformat(),
    )
    archive_old_sessions(sessions, days=7, now=today)

    days = list_sessions_by_day(sessions)

    dates = [d["date"] for d in days]
    assert dates == ["2026-05-09"]


def test_list_archive_returns_archived_rows(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    today = datetime(2026, 5, 10, tzinfo=timezone.utc)
    _write_session(
        sessions, "2026-04-15-1200",
        started=(today - timedelta(days=25)).isoformat(),
    )
    _write_session(
        sessions, "2026-05-01-0700",
        started=(today - timedelta(days=9)).isoformat(),
    )
    archive_old_sessions(sessions, days=7, now=today)

    rows = list_archive(sessions)

    names = [r["session_id"] for r in rows]
    assert "2026-04-15-1200" in names
    assert "2026-05-01-0700" in names
    # Newest-started first
    assert names[0] == "2026-05-01-0700"
    # Each row tells the operator which YYYY-MM bucket holds the file
    for r in rows:
        assert r["archived_in"] in ("2026-04", "2026-05")


def test_list_archive_empty_when_archive_missing(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    assert list_archive(sessions) == []


def test_archive_skips_destination_collision(tmp_path: Path) -> None:
    """If a same-named file already lives in the archive (operator
    pre-staged it manually), don't clobber — leave both in place and
    log a warning."""
    sessions = tmp_path / "sessions"
    today = datetime(2026, 5, 10, tzinfo=timezone.utc)
    _write_session(
        sessions, "2026-04-15-1200",
        started=(today - timedelta(days=25)).isoformat(),
    )
    # Pre-stage a stub at the destination
    archive_dir = sessions / "archive" / "2026-04"
    archive_dir.mkdir(parents=True)
    (archive_dir / "2026-04-15-1200.json").write_text("{}", encoding="utf-8")

    moved = archive_old_sessions(sessions, days=7, now=today)

    assert moved == []
    # Original file stays put
    assert (sessions / "2026-04-15-1200.json").exists()
