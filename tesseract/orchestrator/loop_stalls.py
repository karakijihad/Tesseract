"""What the event loop was doing while it was blocked, kept where a reader can
find it.

The backend samples the loop thread's stack four times a second, and when a
stall ends it logs a ranked account of what the thread was in. A log line is
not a record: nothing compares it with yesterday, nothing raises a finding
from it, and the Health room said exactly that in its own words for as long as
the log was the only place the numbers went. So a twelve second block, the
kind that gets the backend killed and respawned, produced a warning nobody was
watching and no evidence afterwards.

One dated file per day, appended to, aged by `retention.yaml::loop_stalls`.
The directory resolves at call time rather than at import, so a test can point
`TESSERACT_HOME` somewhere else and this writes there.

`doing` is a ranked list of frames from this process's own stacks. It reaches
the operator's files and the panel; it never reaches a model, which is what
`Finding.quotable` staying false in the watchman's collector settles.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.lib.clock import today
from tesseract.lib.log_envelope import BAD, WARN, envelope, read_when
from tesseract.supervisor.daemon import _HEARTBEAT_TIMEOUT_S

STREAM = "loop-stalls"

# The word the watchman files these under. It has no resolution rule on
# purpose: nothing on disk says a block that already happened has since become
# untrue, and a kind with no rule survives, which is the safe direction.
KIND = "loop_stalled"

# Where a block stops being a slowdown and becomes a liveness problem: the
# supervisor's health probe gives up at this point, so a block at least this
# long costs a heartbeat, and enough of them in a row get the backend killed
# and respawned.
#
# Imported rather than copied, private name and all. The number is the
# supervisor's and a second one written here would grade a fatal stall as a
# slow one the day somebody tuned the probe. `daemon.py` does nothing at
# import beyond defining constants and classes, and imports nothing from this
# package, so there is no cost and no cycle.
BAD_AFTER_S = _HEARTBEAT_TIMEOUT_S


def root() -> Path:
    from tesseract.paths import log_dir

    return log_dir("loop-stalls")


def path_for(day: date) -> Path:
    """`stalls-YYYY-MM-DD.jsonl`. Dated in the name, like every other artifact
    a retention sweep ages, because an mtime makes a file a backup touched
    look young forever. The day is the operator's, not UTC's, which is what
    the sweep's own `date.today()` cutoff is comparing against."""
    return root() / f"stalls-{day.isoformat()}.jsonl"


def record(*, blocked_for_s: float, doing: str) -> None:
    """Append one stall.

    Blocking file IO by design, and the caller's job to keep it off the event
    loop: this is called from a handler that runs ON that loop, and a write
    that stalls the thread it is describing is its own next record.
    """
    row = {
        **envelope(
            stream=STREAM,
            # The writer grades it, and every reader downstream takes the word
            # rather than re-deciding from the number. Two answers to how bad
            # a stall is is how a room and a report come to disagree about the
            # same twelve seconds.
            severity=BAD if blocked_for_s >= BAD_AFTER_S else WARN,
            subject="event loop",
            summary=f"the app was blocked for {blocked_for_s:.1f} seconds",
            detail={"seconds": round(blocked_for_s, 3)},
        ),
        "kind": KIND,
        "seconds": round(blocked_for_s, 3),
        "doing": doing,
    }
    directory = root()
    directory.mkdir(parents=True, exist_ok=True)
    with path_for(today()).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def read_since(start: datetime | None, end: datetime) -> list[dict[str, Any]]:
    """Every stall recorded in the window, oldest first.

    A row that cannot be parsed or dated is skipped rather than raising: this
    file is evidence, and one bad line must not take the whole account away
    from the reader that came for the other twenty.
    """
    directory = root()
    if not directory.is_dir():
        return []
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for path in sorted(directory.glob("stalls-*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            when = read_when(row)
            if when is None:
                continue
            if start is not None and when < start:
                continue
            if when > end:
                continue
            rows.append((when, row))
    rows.sort(key=lambda pair: pair[0])
    return [row for _when, row in rows]


def latest() -> dict[str, Any] | None:
    """The most recent stall on record, or `None` if none was ever written."""
    directory = root()
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("stalls-*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                return row
    return None


__all__ = [
    "BAD_AFTER_S", "KIND", "STREAM", "latest", "path_for", "read_since",
    "record", "root",
]
