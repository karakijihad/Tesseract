"""Heartbeat write + staleness check for durable workers.

A running worker touches ``<worker_dir>/heartbeat`` every 30s. The
RecoveryManager scans heartbeats on boot: mtime older than 90s OR PID
dead → ``interrupted``. The file is mtime-only (zero bytes) per the
schema contract — no payload needed, the mtime IS the heartbeat.

The 30s / 90s pair is locked in
``_shared/worker-record-schema.md §Heartbeat / staleness`` and lives here
as constants — there is no YAML key for either, and an earlier version of
this docstring wrongly sent operators looking for one. Changing them means
changing the schema and this module together, since recovery's staleness
verdict and the writer's interval have to stay a 1:3 pair.

``liveness.announce_stale_workers`` takes a ``threshold`` argument, which
moves only what IT reports; it does not move the recovery threshold below.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from tesseract.orchestrator.workers.paths import worker_dir

log = logging.getLogger(__name__)


HEARTBEAT_INTERVAL_SECONDS: float = 30.0
"""How often a running worker SHOULD touch its heartbeat file."""

STALENESS_THRESHOLD_SECONDS: float = 90.0
"""Mtime older than this AND/OR PID-dead → recovery marks interrupted.
Three missed touches (30 × 3 = 90) is the schema-locked threshold."""


def heartbeat_path(worker_id: str) -> Path:
    return worker_dir(worker_id) / "heartbeat"


def touch_heartbeat(worker_id: str, *, now: float | None = None) -> Path:
    """Set the heartbeat file's mtime to now. Creates an empty file on
    first call; subsequent calls update mtime only. Parent dir is
    created if missing — but in practice ``write_record`` runs first
    and creates ``<worker_dir>/`` so the heartbeat lands beside it.
    """
    path = heartbeat_path(worker_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open-for-append is create-if-missing AND a no-op if present —
    # one syscall instead of the stat+touch+utime sequence, no TOCTOU
    # window between the existence check and the create.
    with path.open("a"):
        pass
    stamp = now if now is not None else time.time()
    os.utime(path, (stamp, stamp))
    return path


def read_heartbeat_mtime(worker_id: str) -> float | None:
    """Return the heartbeat file's mtime (seconds since epoch). ``None``
    if the file doesn't exist — recovery treats that as "never heard
    from", same outcome as a stale beat."""
    path = heartbeat_path(worker_id)
    if not path.exists():
        return None
    try:
        return path.stat().st_mtime
    except OSError as exc:
        log.warning("heartbeat stat failed for %s: %s", worker_id, exc)
        return None


def is_heartbeat_stale(
    worker_id: str,
    *,
    now: float | None = None,
    threshold: float = STALENESS_THRESHOLD_SECONDS,
) -> bool:
    """True if heartbeat is missing OR older than ``threshold`` seconds."""
    mtime = read_heartbeat_mtime(worker_id)
    if mtime is None:
        return True
    current = now if now is not None else time.time()
    return (current - mtime) > threshold


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "STALENESS_THRESHOLD_SECONDS",
    "heartbeat_path",
    "is_heartbeat_stale",
    "read_heartbeat_mtime",
    "touch_heartbeat",
]
