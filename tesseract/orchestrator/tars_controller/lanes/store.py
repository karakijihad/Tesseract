"""Atomic on-disk persistence for `lane.json` records.

Roots are resolved at call time from `TESSERACT_HOME` so a test-time
``monkeypatch.setenv("TESSERACT_HOME", tmp)`` reaches every writer
without re-importing this module."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ._common import home_root
from .models import Lane


def lanes_root() -> Path:
    return home_root() / "controller" / "lanes"


def archive_root() -> Path:
    return home_root() / "controller" / "lanes-archive"


def lane_dir(lane_id: str) -> Path:
    return lanes_root() / lane_id


def _lane_json_path(lane_id: str) -> Path:
    return lane_dir(lane_id) / "lane.json"


def write_lane(lane: Lane) -> Path:
    """Atomic write of `lane.json` via tmpfile + os.replace.

    The lane's owning directory is created if missing. Returns the
    final path so callers can chain (e.g. for logging)."""
    directory = lane_dir(lane.lane_id)
    directory.mkdir(parents=True, exist_ok=True)
    final = _lane_json_path(lane.lane_id)
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(
        lane.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, final)
    return final


def read_lane(lane_id: str) -> Lane:
    path = _lane_json_path(lane_id)
    if not path.exists():
        raise FileNotFoundError(f"lane.json missing for lane_id={lane_id}")
    return Lane.model_validate_json(path.read_text(encoding="utf-8"))


def list_lane_ids() -> list[str]:
    """Every subdirectory of `lanes_root()` that contains a `lane.json`
    is reported. Orphan directories without the record are skipped so
    a half-initialised lane (mkdir succeeded, atomic write didn't) is
    not surfaced as live."""
    root = lanes_root()
    if not root.exists():
        return []
    out: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "lane.json").exists():
            out.append(entry.name)
    return out


def archive_lane(lane_id: str) -> Path:
    """Move `<lanes_root>/<lane_id>/` to
    `<archive_root>/<YYYY-MM>/<lane_id>/`. Returns the new path. The
    four contract files (`lane.json`, `events.jsonl`, `transcript.txt`,
    `last_cursor.txt`) move together — `shutil.move` is recursive so
    any future sidecars also migrate without code changes here.

    Safe to call on a directory that is already a partial archive
    target — `os.replace` overwrites; we use it on the leaf rename
    so re-archiving an id (rare) succeeds rather than raising."""
    src = lane_dir(lane_id)
    if not src.exists():
        raise FileNotFoundError(f"cannot archive missing lane {lane_id}")
    bucket = datetime.now(timezone.utc).strftime("%Y-%m")
    dest_parent = archive_root() / bucket
    dest_parent.mkdir(parents=True, exist_ok=True)
    dest = dest_parent / lane_id
    if dest.exists():
        # Re-archive — replace the prior copy.
        shutil.rmtree(dest)
    shutil.move(str(src), str(dest))
    return dest


__all__ = [
    "archive_lane",
    "archive_root",
    "lane_dir",
    "lanes_root",
    "list_lane_ids",
    "read_lane",
    "write_lane",
]
