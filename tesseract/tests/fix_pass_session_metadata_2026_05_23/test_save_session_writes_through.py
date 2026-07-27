"""End-to-end: save_session populates the metadata index; the listing
paths consume it; delete / rename / archive keep it in sync.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.brain.session_store import (
    archive_old_sessions,
    delete_session,
    list_archive,
    list_sessions_by_day,
    rename_session,
    save_session,
)
from tesseract.memory.session_metadata import SessionMetadataIndex


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


def test_save_session_populates_metadata_index(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    save_session(
        sessions,
        name="2026-05-23-1024",
        model="fake",
        started_at="2026-05-23T10:24:00+00:00",
        history=[{"role": "user", "content": "hi"}],
    )
    idx = SessionMetadataIndex(tmp_path / "session_metadata.sqlite")
    assert idx.count() == 1
    days = idx.list_active_by_day()
    assert days[0]["runs"][0]["session_id"] == "2026-05-23-1024"


def test_list_sessions_by_day_reads_from_index(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    save_session(sessions, name="2026-05-23-0900", model="x",
                 started_at="2026-05-23T09:00:00+00:00",
                 history=[{"role": "user", "content": "a"}])
    save_session(sessions, name="2026-05-23-1500", model="x",
                 started_at="2026-05-23T15:00:00+00:00",
                 history=[{"role": "user", "content": "b"}])
    days = list_sessions_by_day(sessions)
    assert len(days) == 1
    assert days[0]["date"] == "2026-05-23"
    assert days[0]["run_count"] == 2


def test_delete_session_drops_metadata_row(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    save_session(sessions, name="2026-05-23-1024", model="x",
                 started_at="2026-05-23T10:24:00+00:00",
                 history=[{"role": "user", "content": "a"}])
    ok, reason = delete_session(sessions, "2026-05-23-1024")
    assert ok and reason == ""
    idx = SessionMetadataIndex(tmp_path / "session_metadata.sqlite")
    assert idx.count() == 0


def test_rename_session_updates_metadata(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    save_session(sessions, name="2026-05-23-1024", model="x",
                 started_at="2026-05-23T10:24:00+00:00",
                 history=[{"role": "user", "content": "a"}])
    ok, reason = rename_session(sessions, "2026-05-23-1024", "renamed")
    assert ok, reason
    idx = SessionMetadataIndex(tmp_path / "session_metadata.sqlite")
    days = idx.list_active_by_day()
    flat_ids = {r["session_id"] for d in days for r in d["runs"]}
    assert "renamed" in flat_ids
    assert "2026-05-23-1024" not in flat_ids


def test_archive_old_sessions_flips_metadata(tmp_path: Path) -> None:
    """`archive_old_sessions` must move the file AND set ``archived_in``
    in the metadata index so the row drops out of the active list."""
    from datetime import datetime, timedelta, timezone

    sessions = tmp_path / "sessions"
    # Save with a backdated filename so archive cutoff triggers.
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d-%H%M")
    save_session(sessions, name=old, model="x",
                 started_at="2026-04-22T10:00:00+00:00",
                 history=[{"role": "user", "content": "old"}])
    moved = archive_old_sessions(sessions, days=7)
    assert len(moved) == 1
    days = list_sessions_by_day(sessions)
    # Active list no longer contains the archived session.
    flat = {r["session_id"] for d in days for r in d["runs"]}
    assert old not in flat
    # Archive listing surfaces it.
    archive_rows = list_archive(sessions)
    assert any(r["session_id"] == old for r in archive_rows)
