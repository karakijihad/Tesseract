"""Session metadata derived index — schema, upsert, listing, sync.

The Mirror drawer's session-list path used to open every JSON file in
`tesseract/sessions/*.json` and parse each on every render. With ~100
sessions that's noticeable; at archival scale it's worse.

This module mirrors the CR-1 `WorkIndex` pattern: SQLite derived index
populated by the canonical-state mutators in `session_store.py` —
files stay the source of truth, the index is rebuildable from disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.memory.session_metadata import (
    SessionMetadataIndex,
    SessionMetaRow,
)


def test_upsert_and_list_active(tmp_path: Path) -> None:
    idx = SessionMetadataIndex(tmp_path / "sm.sqlite")
    idx.upsert(SessionMetaRow(
        session_id="2026-05-22-1024",
        started_at="2026-05-22T10:24:00+00:00",
        ended_at="2026-05-22T11:30:00+00:00",
        turn_count=12,
        model="gpt-5.4-nano",
        file_path=str(tmp_path / "sessions" / "2026-05-22-1024.json"),
        archived_in=None,
    ))
    idx.upsert(SessionMetaRow(
        session_id="2026-05-22-1500",
        started_at="2026-05-22T15:00:00+00:00",
        ended_at="2026-05-22T15:45:00+00:00",
        turn_count=4,
        model="gpt-5.4-nano",
        file_path=str(tmp_path / "sessions" / "2026-05-22-1500.json"),
        archived_in=None,
    ))
    days = idx.list_active_by_day()
    assert len(days) == 1
    assert days[0]["date"] == "2026-05-22"
    assert days[0]["run_count"] == 2
    # Runs within a day sort newest-first by started_at.
    assert days[0]["runs"][0]["session_id"] == "2026-05-22-1500"
    assert days[0]["total_turns"] == 16


def test_upsert_is_idempotent(tmp_path: Path) -> None:
    idx = SessionMetadataIndex(tmp_path / "sm.sqlite")
    row = SessionMetaRow(
        session_id="2026-05-22-1024",
        started_at="2026-05-22T10:24:00+00:00",
        ended_at="2026-05-22T11:30:00+00:00",
        turn_count=12,
        model="gpt-5.4-nano",
        file_path="/p.json",
    )
    idx.upsert(row)
    idx.upsert(row)
    idx.upsert(row)
    assert idx.count() == 1


def test_delete_removes_row(tmp_path: Path) -> None:
    idx = SessionMetadataIndex(tmp_path / "sm.sqlite")
    idx.upsert(SessionMetaRow(
        session_id="2026-05-22-1024",
        started_at="2026-05-22T10:24:00+00:00",
        ended_at=None,
        turn_count=0,
        model="x",
        file_path="/p.json",
    ))
    assert idx.count() == 1
    idx.delete("2026-05-22-1024")
    assert idx.count() == 0


def test_update_archived(tmp_path: Path) -> None:
    idx = SessionMetadataIndex(tmp_path / "sm.sqlite")
    idx.upsert(SessionMetaRow(
        session_id="2026-04-10-0900",
        started_at="2026-04-10T09:00:00+00:00",
        ended_at="2026-04-10T09:30:00+00:00",
        turn_count=3,
        model="x",
        file_path="/p.json",
        archived_in=None,
    ))
    # Active before, archive moves it out of the active list.
    assert idx.list_active_by_day()[0]["date"] == "2026-04-10"
    idx.update_archived("2026-04-10-0900", "2026-04", "/archive/p.json")
    assert idx.list_active_by_day() == []
    archive_rows = idx.list_archive()
    assert len(archive_rows) == 1
    assert archive_rows[0]["archived_in"] == "2026-04"


def test_rename(tmp_path: Path) -> None:
    idx = SessionMetadataIndex(tmp_path / "sm.sqlite")
    idx.upsert(SessionMetaRow(
        session_id="before-rebase",
        started_at="2026-05-22T10:00:00+00:00",
        ended_at=None,
        turn_count=0,
        model="x",
        file_path="/sessions/before-rebase.json",
    ))
    idx.rename("before-rebase", "renamed", "/sessions/renamed.json")
    assert idx.count() == 1
    days = idx.list_active_by_day()
    assert days[0]["runs"][0]["session_id"] == "renamed"


def test_custom_dates_land_in_custom_bucket(tmp_path: Path) -> None:
    """Sessions whose name doesn't match YYYY-MM-DD-HHMM should group
    under a synthetic `custom` date bucket."""
    idx = SessionMetadataIndex(tmp_path / "sm.sqlite")
    idx.upsert(SessionMetaRow(
        session_id="before-rebase",
        started_at="2026-05-22T10:00:00+00:00",
        ended_at=None,
        turn_count=0,
        model="x",
        file_path="/p.json",
    ))
    days = idx.list_active_by_day()
    assert any(d["date"] == "custom" for d in days)


def test_rebuild_from_disk(tmp_path: Path) -> None:
    """Backfill walks the sessions dir + archive subtree and populates
    rows for every JSON it can parse."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "2026-05-22-1024.json").write_text(
        '{"schema":1,"started_at":"2026-05-22T10:24:00+00:00",'
        '"ended_at":"2026-05-22T11:30:00+00:00","turn_count":7,'
        '"model":"fake","history":[]}',
        encoding="utf-8",
    )
    archive = sessions / "archive" / "2026-04"
    archive.mkdir(parents=True)
    (archive / "2026-04-10-0900.json").write_text(
        '{"schema":1,"started_at":"2026-04-10T09:00:00+00:00",'
        '"ended_at":"2026-04-10T09:30:00+00:00","turn_count":3,'
        '"model":"fake","history":[]}',
        encoding="utf-8",
    )
    idx = SessionMetadataIndex(tmp_path / "sm.sqlite")
    n = idx.rebuild_from_disk(sessions)
    assert n == 2
    assert len(idx.list_active_by_day()) == 1
    assert idx.list_active_by_day()[0]["run_count"] == 1
    assert idx.list_archive()[0]["archived_in"] == "2026-04"


def test_rebuild_is_idempotent_and_resets_stale(tmp_path: Path) -> None:
    """Rebuilding drops orphan rows whose file no longer exists."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "good.json").write_text(
        '{"schema":1,"started_at":"2026-05-22T10:00:00+00:00",'
        '"ended_at":null,"turn_count":1,"model":"x","history":[]}',
        encoding="utf-8",
    )
    idx = SessionMetadataIndex(tmp_path / "sm.sqlite")
    # Pre-seed a row whose file does NOT exist (simulates a deleted
    # session that the index didn't catch via the delete hook).
    idx.upsert(SessionMetaRow(
        session_id="gone",
        started_at="2026-05-22T09:00:00+00:00",
        ended_at=None,
        turn_count=0,
        model="x",
        file_path=str(sessions / "gone.json"),
    ))
    n = idx.rebuild_from_disk(sessions)
    assert n == 1
    # The `gone` row was wiped because rebuild drops all rows before
    # re-walking.
    ids = {r["session_id"] for d in idx.list_active_by_day() for r in d["runs"]}
    assert ids == {"good"}


def test_prune_orphans_drops_rows_whose_file_is_missing(tmp_path: Path) -> None:
    """Lighter-touch maintenance: prune in place rather than full rebuild."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "live.json").write_text(
        '{"schema":1,"started_at":"2026-05-22T10:00:00+00:00",'
        '"ended_at":null,"turn_count":1,"model":"x","history":[]}',
        encoding="utf-8",
    )
    idx = SessionMetadataIndex(tmp_path / "sm.sqlite")
    idx.upsert(SessionMetaRow(
        session_id="live", started_at="2026-05-22T10:00:00+00:00",
        ended_at=None, turn_count=1, model="x",
        file_path=str(sessions / "live.json"),
    ))
    idx.upsert(SessionMetaRow(
        session_id="ghost", started_at="2026-05-22T09:00:00+00:00",
        ended_at=None, turn_count=0, model="x",
        file_path=str(sessions / "ghost.json"),  # never on disk
    ))
    assert idx.count() == 2
    pruned = idx.prune_orphans()
    assert pruned == 1
    assert idx.count() == 1
