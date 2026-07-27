"""Tests for the autonomy prune ledger (Task 1.3).

Every test points ``TESSERACT_HOME`` at ``tmp_path`` before calling any
ledger function so nothing lands under the real ``tesseract/logs/`` tree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.autonomy.prune_ledger import (
    PruneRecord,
    PruneStage,
    prune_counts,
    read_prunes,
    record_prune,
)


def _record(
    *,
    source: AgendaSource = AgendaSource.SELF_REFLECTION,
    stage: PruneStage = PruneStage.LOW_VALUE,
    goal: str = "Jane Doe requested a low value follow-up",
    ts: datetime | None = None,
) -> PruneRecord:
    return PruneRecord(
        source=source,
        goal=goal,
        stage=stage,
        reason="score below threshold",
        ts=ts or datetime.now(timezone.utc),
    )


def test_record_prune_roundtrips_through_read_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    rec = _record()

    record_prune(rec)
    got = read_prunes()

    assert len(got) == 1
    assert got[0].source == rec.source
    assert got[0].goal == rec.goal
    assert got[0].stage == rec.stage
    assert got[0].reason == rec.reason


def test_read_prunes_returns_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    now = datetime.now(timezone.utc)

    oldest = _record(goal="oldest", ts=now - timedelta(hours=2))
    middle = _record(goal="middle", ts=now - timedelta(hours=1))
    newest = _record(goal="newest", ts=now)

    for rec in (oldest, middle, newest):
        record_prune(rec)

    got = read_prunes()

    assert [r.goal for r in got] == ["newest", "middle", "oldest"]


def test_read_prunes_limit_caps_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    for i in range(5):
        record_prune(_record(goal=f"item-{i}"))

    got = read_prunes(limit=2)

    assert len(got) == 2


def test_prune_counts_buckets_by_source_and_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    now = datetime.now(timezone.utc)

    record_prune(
        _record(source=AgendaSource.SELF_REFLECTION, stage=PruneStage.LOW_VALUE, ts=now)
    )
    record_prune(
        _record(source=AgendaSource.SELF_REFLECTION, stage=PruneStage.LOW_VALUE, ts=now)
    )
    record_prune(
        _record(source=AgendaSource.SELF_REFLECTION, stage=PruneStage.DUPLICATE, ts=now)
    )
    record_prune(
        _record(source=AgendaSource.PROVIDER_WATCH, stage=PruneStage.MALFORMED, ts=now)
    )
    # Older than the window — must be excluded.
    record_prune(
        _record(
            source=AgendaSource.SELF_REFLECTION,
            stage=PruneStage.LOW_VALUE,
            ts=now - timedelta(hours=48),
        )
    )

    counts = prune_counts(window_hours=24)

    assert counts == {
        "self_reflection": {"low_value": 2, "duplicate": 1},
        "provider_watch": {"malformed": 1},
    }


def test_missing_file_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    assert read_prunes() == []
    assert prune_counts(window_hours=24) == {}


def test_corrupt_line_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    record_prune(_record(goal="good record"))

    path = tmp_path / "logs" / "autonomy" / "pruned.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not-json-garbage\n")

    record_prune(_record(goal="another good record"))

    got = read_prunes()

    assert [r.goal for r in got] == ["another good record", "good record"]
