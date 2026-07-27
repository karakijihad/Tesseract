"""X-4 Session A — `store.write_lane` / `read_lane` / `list_lane_ids`
/ `archive_lane` round-trips against an isolated `TESSERACT_HOME`."""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.orchestrator.tars_controller.lanes import (
    Lane,
    archive_lane,
    lane_dir,
    lanes_root,
    list_lane_ids,
    read_lane,
    write_lane,
)


def _make_lane(lane_id: str = "lane-claude-x4test01") -> Lane:
    return Lane(
        lane_id=lane_id,
        kind="claude",
        mode="headless",
        model="claude-sonnet-4-6",
        working_dir="/tmp/proj",
    )


def test_write_then_read_round_trip(isolated_home: Path) -> None:
    lane = _make_lane()
    write_lane(lane)
    parsed = read_lane(lane.lane_id)
    assert parsed == lane


def test_write_is_atomic_via_replace(isolated_home: Path) -> None:
    """After write, no `.json.tmp` should be left behind."""
    lane = _make_lane()
    write_lane(lane)
    directory = lane_dir(lane.lane_id)
    files = sorted(p.name for p in directory.iterdir())
    assert "lane.json" in files
    assert all(not f.endswith(".tmp") for f in files)


def test_list_lane_ids_returns_only_lanes_with_records(
    isolated_home: Path,
) -> None:
    lane_a = _make_lane("lane-claude-aaa")
    lane_b = _make_lane("lane-claude-bbb")
    write_lane(lane_a)
    write_lane(lane_b)
    # Create an orphan directory without lane.json — must NOT appear.
    (lanes_root() / "lane-claude-orphan").mkdir(parents=True, exist_ok=True)
    ids = list_lane_ids()
    assert ids == ["lane-claude-aaa", "lane-claude-bbb"]


def test_read_lane_raises_when_missing(isolated_home: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_lane("lane-claude-never-written")


def test_archive_lane_moves_dir_to_archive_root(isolated_home: Path) -> None:
    lane = _make_lane()
    write_lane(lane)
    # Drop a sibling file so we can assert it moved too.
    (lane_dir(lane.lane_id) / "transcript.txt").write_text(
        "hello", encoding="utf-8"
    )

    dest = archive_lane(lane.lane_id)

    assert not lane_dir(lane.lane_id).exists()
    assert dest.exists()
    assert (dest / "lane.json").exists()
    assert (dest / "transcript.txt").read_text(encoding="utf-8") == "hello"
    # Archive path is under <home>/controller/lanes-archive/<YYYY-MM>/<id>/
    assert dest.parent.parent.name == "lanes-archive"


def test_archive_overwrites_existing_archive(isolated_home: Path) -> None:
    """Re-archiving the same id is rare but must succeed (the prior
    archive copy is replaced)."""
    lane = _make_lane()
    write_lane(lane)
    archive_lane(lane.lane_id)

    # Re-create a fresh lane with the same id and archive it again.
    write_lane(lane)
    dest = archive_lane(lane.lane_id)
    assert dest.exists()
    assert (dest / "lane.json").exists()
