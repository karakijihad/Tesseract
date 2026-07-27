"""Daily sweep job — prune orphans from session_metadata + work_index.

End-to-end: pre-seed both indexes with rows that DO and DON'T have
files on disk, run the job, assert only the missing-file rows were
dropped. The job never raises (BaseJob contract).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tesseract.memory.session_metadata import (
    SessionMetadataIndex,
    SessionMetaRow,
)
from tesseract.memory.work_index import WorkChunk, WorkIndex
from tesseract.scheduler.tasks.work_index_sweep import WorkIndexSweepJob
from tesseract.scheduler.types import JobContext


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    """Set up a sessions dir with one live + one ghost row in each index."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    live_path = sessions / "live.json"
    live_path.write_text(json.dumps({
        "schema": 1,
        "started_at": "2026-05-23T10:00:00+00:00",
        "ended_at": None,
        "turn_count": 1,
        "model": "x",
        "history": [],
    }), encoding="utf-8")
    ghost_path = sessions / "ghost.json"  # NOT created — orphan target

    sm = SessionMetadataIndex(tmp_path / "session_metadata.sqlite")
    sm.upsert(SessionMetaRow(
        session_id="live", started_at="2026-05-23T10:00:00+00:00",
        ended_at=None, turn_count=1, model="x",
        file_path=str(live_path),
    ))
    sm.upsert(SessionMetaRow(
        session_id="ghost", started_at="2026-05-23T09:00:00+00:00",
        ended_at=None, turn_count=0, model="x",
        file_path=str(ghost_path),
    ))
    sm.close()

    wi = WorkIndex(tmp_path / "work_index.sqlite")
    wi.add(WorkChunk(
        source="session", source_path=str(live_path), source_ref="live",
        turn_idx=0, role="user", chunk_idx=0, ts="2026-05-23", text="alive",
    ))
    wi.add(WorkChunk(
        source="session", source_path=str(ghost_path), source_ref="ghost",
        turn_idx=0, role="user", chunk_idx=0, ts="2026-05-23", text="gone",
    ))
    wi.close()
    return sessions, tmp_path


@pytest.mark.asyncio
async def test_sweep_prunes_only_orphans(tmp_path: Path) -> None:
    _seed(tmp_path)
    ctx = JobContext(job_name="work_index_sweep", app=None)
    result = await WorkIndexSweepJob().run(ctx)

    assert result.ok
    # session_metadata had 1 orphan + 1 live → 1 dropped, 1 kept.
    assert result.payload["session_metadata_pruned"] == 1
    # work_index had 1 orphan path + 1 live path → 1 path dropped.
    assert result.payload["work_index_paths_pruned"] == 1

    sm = SessionMetadataIndex(tmp_path / "session_metadata.sqlite")
    assert sm.count() == 1
    sm.close()
    wi = WorkIndex(tmp_path / "work_index.sqlite")
    assert wi.count() == 1
    wi.close()


@pytest.mark.asyncio
async def test_sweep_is_idempotent(tmp_path: Path) -> None:
    """Running twice on a clean state drops nothing the second time."""
    _seed(tmp_path)
    job = WorkIndexSweepJob()
    ctx = JobContext(job_name="work_index_sweep", app=None)
    first = await job.run(ctx)
    second = await job.run(ctx)
    assert first.ok and second.ok
    assert second.payload["session_metadata_pruned"] == 0
    assert second.payload["work_index_paths_pruned"] == 0


@pytest.mark.asyncio
async def test_sweep_missing_indexes_safe(tmp_path: Path) -> None:
    """No sqlite files on disk → job creates them empty and reports zero."""
    ctx = JobContext(job_name="work_index_sweep", app=None)
    result = await WorkIndexSweepJob().run(ctx)
    assert result.ok
    assert result.payload["session_metadata_pruned"] == 0
    assert result.payload["work_index_paths_pruned"] == 0
