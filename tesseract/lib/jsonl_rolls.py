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


__all__ = ["rewrite", "row_time"]
