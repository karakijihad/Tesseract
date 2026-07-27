"""Phase 2 (e) — observer log retention.

`tesseract/logs/observer/YYYY-MM-DD.jsonl` accumulates one record per
observation. Without retention they grow without bound. The prune job
deletes files older than `retention_days` (default 14) on cron.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path

from tesseract.scheduler.tasks.observer_log_prune import (
    ObserverLogPruneJob,
    _prune_old_logs,
)
from tesseract.scheduler.types import JobContext


def _seed_logs(log_dir: Path, days: list[int]) -> list[Path]:
    """Create JSONL files dated `today - N` for each N in days. Returns
    the created paths in the same order so tests can reason about
    deletion."""
    log_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    paths: list[Path] = []
    for offset in days:
        day = today - timedelta(days=offset)
        path = log_dir / f"{day.isoformat()}.jsonl"
        path.write_text(
            '{"timestamp":"2026-04-30T00:00:00Z","mode":"meta","session_id":"","text":"x"}\n',
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def test_prune_no_op_when_dir_missing(tmp_path: Path) -> None:
    log_dir = tmp_path / "observer"
    deleted = _prune_old_logs(log_dir, retention_days=14)
    assert deleted == 0


def test_prune_keeps_recent_deletes_old(tmp_path: Path) -> None:
    log_dir = tmp_path / "observer"
    today_p, recent_p, old_p, ancient_p = _seed_logs(log_dir, [0, 5, 20, 365])
    deleted = _prune_old_logs(log_dir, retention_days=14)
    assert deleted == 2
    assert today_p.exists()
    assert recent_p.exists()
    assert not old_p.exists()
    assert not ancient_p.exists()


def test_prune_skips_non_date_filenames(tmp_path: Path) -> None:
    """Filenames whose stem is not YYYY-MM-DD are ignored — reserves the
    convention for future promoted-and-renamed files (mirrors the diary
    promotion pattern)."""
    log_dir = tmp_path / "observer"
    log_dir.mkdir(parents=True, exist_ok=True)
    keep = log_dir / "promoted-archive.jsonl"
    keep.write_text("{}\n", encoding="utf-8")
    _seed_logs(log_dir, [365])
    deleted = _prune_old_logs(log_dir, retention_days=14)
    assert deleted == 1
    assert keep.exists()


def test_prune_boundary_includes_cutoff_day(tmp_path: Path) -> None:
    """A file dated exactly `retention_days` ago must NOT be deleted —
    keeps the boundary inclusive on the keep side."""
    log_dir = tmp_path / "observer"
    boundary, = _seed_logs(log_dir, [14])
    deleted = _prune_old_logs(log_dir, retention_days=14)
    assert deleted == 0
    assert boundary.exists()


def test_job_run_returns_ok_with_count(tmp_path: Path) -> None:
    log_dir = tmp_path / "observer"
    _seed_logs(log_dir, [0, 30])
    job = ObserverLogPruneJob()
    ctx = JobContext(
        job_name="observer_log_prune",
        app={"observer_log_dir": log_dir},
        config={"retention_days": 7},
    )
    result = asyncio.run(job.run(ctx))
    assert result.ok is True
    assert result.payload["deleted"] == 1
    assert result.payload["retention_days"] == 7


def test_job_run_uses_default_retention_when_unconfigured(tmp_path: Path) -> None:
    log_dir = tmp_path / "observer"
    _seed_logs(log_dir, [20])
    job = ObserverLogPruneJob()
    ctx = JobContext(
        job_name="observer_log_prune",
        app={"observer_log_dir": log_dir},
        config={},
    )
    result = asyncio.run(job.run(ctx))
    assert result.ok is True
    # Default retention is 14 days; a 20-day-old file should be deleted.
    assert result.payload["deleted"] == 1
