"""Rewriting one JSONL file that a single writer owns.

Two files in this runtime are append-only JSONL that a retention pass has to
partition and rewrite: the permission ledger (`permissions/approval_log.py`)
and the scheduler's run log (`scheduler/log.py`). The PARTITION differs — one
archives by month and never drops a row, the other prunes a day once something
has summarised it — and each roll stays on the module that owns its file's
lock, because a read-partition-rewrite from anywhere else loses a row appended
between the read and the replace.

What does NOT differ, and touches no lock, is reading a row's timestamp and
replacing the file atomically. Those live here so a fix to the crash-safety
pattern reaches both.

`prune_older_than` is for the other case: an append-only JSONL that NO lock
protects, because nothing ever pruned it and its writer never needed one. Two
of those are written by the supervisor process while the sweep runs in the
backend, so a lock on either side would not join them. It stats the file before
the read and again before the replace, and abandons the prune when the file
moved in between, so the row the append-during-rewrite would have lost is
simply pruned by the next run instead.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path


def row_time(line: str, field: str) -> datetime | None:
    """The timestamp on one JSONL row, or `None` if it cannot be read.

    `None` is a decision, not an error: both callers KEEP a row they cannot
    date rather than guessing which side of a window it falls on. Nothing is
    lost to housekeeping.
    """
    try:
        stamped = datetime.fromisoformat(json.loads(line)[field])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return stamped if stamped.tzinfo else stamped.replace(tzinfo=timezone.utc)


def rewrite(path: Path, lines: list[str]) -> None:
    """Temp file + `os.replace`, so a crash mid-write leaves the previous file
    whole rather than half of one. The temp name carries the pid and random
    hex so two writers cannot collide on it."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def prune_older_than(path: Path, cutoff: datetime, field: str) -> int:
    """Drop rows dated before `cutoff` from an unlocked JSONL. Returns how many.

    Returns 0 for a file that does not exist, has nothing old enough, or was
    written to while this was reading it. The last of those is the point: the
    two ledgers this was written for are appended to by the supervisor process
    and pruned by the backend, so there is no lock either could take, and a
    plain read-partition-replace loses a row that arrived in between. Here the
    file's size and mtime are read before and after, and a change abandons the
    whole prune rather than writing a file that is missing a row. The next run
    does it, a day later, on a file nobody is touching.

    What remains is the instant between the final stat and the replace, which
    cannot be closed without a lock both processes take. On Windows it is not
    even a lost row: a replace over a file another process holds open fails,
    which the caller reports as a failure rather than as a sweep that worked.

    A row whose timestamp will not parse is KEPT, for the reason `row_time`
    gives: an unreadable date is not a licence to guess which side of the
    window it falls on.
    """
    try:
        before = path.stat()
    except OSError:
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0

    keep: list[str] = []
    removed = 0
    for line in lines:
        if not line.strip():
            continue
        stamped = row_time(line, field)
        if stamped is not None and stamped < cutoff:
            removed += 1
            continue
        keep.append(line)
    if not removed:
        return 0

    try:
        after = path.stat()
    except OSError:
        return 0
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        return 0
    rewrite(path, keep)
    return removed


__all__ = ["prune_older_than", "rewrite", "row_time"]
