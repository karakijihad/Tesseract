"""Atomic rename that survives a concurrent reader.

Every store under ``memory-store/`` writes a sibling tempfile and
``os.replace``s it over the target. On Windows that rename fails outright
with ``PermissionError: [WinError 5]`` while any other handle holds the
target open for reading, so an unsynchronised reader does not merely see a
stale file, it breaks the write. Three sites hit it: ``MemoryIndex._write``
against ``load_raw``, ``LeafStore._atomic_write`` against ``list_in_state``'s
glob scan, and ``topic_tree.append_seal`` against the prompt reader, which
globs the topic tree on every turn and sorts by mtime descending, so the
file being written is by definition the one it opens.

A reader's handle is open for microseconds, so the write only has to wait.
``replace_with_retry`` retries the rename on a bounded backoff and removes
the tempfile when it still cannot land, rather than leaving an orphan
beside the file it failed to become.

**The whole budget is 15ms, and that ceiling is deliberate.** Two of the
three adopters write from the event loop: ``MemoryIndex._write`` on a
``memory_save``, and ``LeafStore._atomic_write`` on EVERY completed turn,
from ``chat.py::_emit_turn_leaf`` in the ``finally`` of ``send``. A backoff
generous enough to be interesting is a backoff that stalls the turn.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3
RETRY_BASE_SECONDS = 0.005


def replace_with_retry(tmp: Path, target: Path) -> None:
    """``os.replace(tmp, target)``, retried while the target is locked.

    Raises the last ``PermissionError`` when every attempt is denied. The
    tempfile is unlinked first, so a caller that logs and carries on does
    not accumulate ``<name>.<pid>.<hex>.tmp`` files on disk.
    """
    for attempt in range(1, MAX_CONSECUTIVE_FAILURES + 1):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == MAX_CONSECUTIVE_FAILURES:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            log.debug(
                "atomic replace: %s is held open, retry %d of %d",
                target,
                attempt,
                MAX_CONSECUTIVE_FAILURES - 1,
            )
            time.sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)))


__all__ = ["MAX_CONSECUTIVE_FAILURES", "replace_with_retry"]
