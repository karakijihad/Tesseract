"""AU-3 — WorkerRecord round-trip + atomic IO + archive."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers import paths as wpaths
from tesseract.orchestrator.workers.record import (
    RiskClass,
    WorkerRecord,
    WorkerStatus,
    archive_record,
    list_active_records,
    load_record,
    mint_worker_id,
    write_record,
)
from tesseract.tests.fix_pass_autonomy_AU_3.conftest import make_record


def test_mint_worker_id_format(isolated_home: Path) -> None:
    when = datetime(2026, 5, 17, 12, 34, tzinfo=timezone.utc)
    wid = mint_worker_id(WorkerKind.CLAUDE_CLI, now=when)
    assert wid.startswith("wk-2026-05-17-1234-claude_cli-")
    assert len(wid.split("-")[-1]) == 6  # 6 hex chars


def test_write_record_round_trip(isolated_home: Path) -> None:
    record = make_record(role="research-doe")
    path = write_record(record)
    assert path.exists()
    assert path.parent == wpaths.worker_dir(record.id)

    loaded = load_record(record.id)
    assert loaded is not None
    assert loaded.id == record.id
    assert loaded.status == WorkerStatus.QUEUED
    assert loaded.risk_class == RiskClass.AUTONOMOUS
    assert loaded.role == "research-doe"


def test_atomic_write_no_partial_file(isolated_home: Path) -> None:
    """The atomic rewrite uses a `.tmp` intermediate and os.replace.
    After a successful write, no `.tmp` file should remain — a crashed
    half-write is detectable post-restart by the orphan. The tmp suffix
    is process+token unique so concurrent writers can't share state."""
    record = make_record()
    write_record(record)
    worker_path = wpaths.worker_dir(record.id)
    record_path = worker_path / "record.json"
    assert record_path.exists()
    leftover_tmps = list(worker_path.glob("*.tmp"))
    assert leftover_tmps == [], f"orphaned tmp files: {leftover_tmps}"


def test_transition_to_appends_history(isolated_home: Path) -> None:
    record = make_record()
    write_record(record)
    record.transition_to(WorkerStatus.RUNNING, reason="lane_admitted")
    write_record(record)

    loaded = load_record(record.id)
    assert loaded is not None
    assert loaded.status == WorkerStatus.RUNNING
    assert len(loaded.status_history) == 1
    entry = loaded.status_history[0]
    assert entry.from_status == "queued"
    assert entry.to_status == "running"
    assert entry.reason == "lane_admitted"


def test_transition_noop_when_same_status(isolated_home: Path) -> None:
    record = make_record(status=WorkerStatus.RUNNING)
    record.transition_to(WorkerStatus.RUNNING, reason="ignored")
    assert record.status_history == []


def test_load_record_missing_returns_none(isolated_home: Path) -> None:
    assert load_record("wk-never-existed") is None


def test_list_active_records_skips_malformed(isolated_home: Path) -> None:
    record = make_record()
    write_record(record)

    # Inject a malformed neighbor — must not crash the listing.
    bad_dir = wpaths.workers_active_dir() / "wk-broken-doe"
    bad_dir.mkdir(parents=True)
    (bad_dir / "record.json").write_text("{not-json", encoding="utf-8")

    records = list_active_records()
    assert [r.id for r in records] == [record.id]


def test_extra_fields_rejected(isolated_home: Path) -> None:
    """`extra="forbid"` makes a typo'd field a load-time error rather
    than a silently-dropped attribute."""
    record = make_record()
    raw = json.loads(record.model_dump_json())
    raw["typo_field"] = "nope"
    with pytest.raises(ValidationError):
        WorkerRecord.model_validate(raw)


def test_archive_record_moves_directory(isolated_home: Path) -> None:
    record = make_record(status=WorkerStatus.RUNNING)
    write_record(record)
    record.transition_to(WorkerStatus.DONE, reason="ok")
    write_record(record)

    active = wpaths.worker_dir(record.id)
    assert active.exists()

    dst = archive_record(record)
    assert dst.exists()
    assert not active.exists()
    assert "archive" in dst.parts
    # YYYY-MM bucket from updated_at — UTC normalization keeps it stable.
    assert record.updated_at.astimezone(timezone.utc).strftime("%Y-%m") in dst.parts


def test_archive_refuses_non_terminal(isolated_home: Path) -> None:
    record = make_record(status=WorkerStatus.RUNNING)
    write_record(record)
    with pytest.raises(ValueError):
        archive_record(record)


def test_load_record_reads_from_archive(isolated_home: Path) -> None:
    record = make_record(status=WorkerStatus.DONE)
    write_record(record)
    archive_record(record)
    loaded = load_record(record.id)
    assert loaded is not None
    assert loaded.id == record.id
