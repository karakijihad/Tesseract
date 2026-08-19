"""Notice a worker that stopped beating, while the app is still running.

Recovery already detects stale workers, but only on the next boot, and only
to report that something died hours ago. The operator's symptom was the gap
in between: *"it made this tasks, and nothing fired"* — a task list that
stayed unticked with the app open and nothing saying why.

This closes that gap by reading only what is already on disk: a worker in a
non-terminal status whose heartbeat has gone stale is logged at WARNING, and
this module is on the log forwarder's ``_ELEVATED_LOGGERS`` list so that
warning reaches the operator's pulse. WARNING because that is what it is — a
stale worker is a true operational fact and a false error, and claiming ERROR
to reach the pulse put it in that feed's ``errorsOnly`` view beside real
crashes. No new surface, no transition — recovery still owns status changes
at boot. Reporting and reaping are deliberately separate: a heartbeat can lag
because the event loop stalled under a GIL-holding model load, and losing a
live worker to that would be worse than the silence this replaces.

Each worker is announced once per process. The alternative — re-announcing
every wake — is how a pulse feed becomes something the operator learns to
ignore, which is the same failure this exists to prevent.

It reads worker records rather than being read by them, which is why it lives
in this package and not beside a scheduler job. It used to BE a scheduler
job on its own ``*/5`` row; the kernel wakes at least that often and already
walks worker records in the same wake, so the check rides that walk instead of
holding a clock of its own.
"""

from __future__ import annotations

import logging
import time

from tesseract.orchestrator.workers.heartbeat import (
    STALENESS_THRESHOLD_SECONDS,
    is_heartbeat_stale,
    read_heartbeat_mtime,
)
from tesseract.orchestrator.workers.record import (
    TERMINAL_STATUSES,
    list_active_records,
)

log = logging.getLogger(__name__)

# Worker ids already announced this process. Module-level so it survives
# whatever object is doing the walking.
_announced: set[str] = set()


def reset_announced_for_tests() -> None:
    _announced.clear()


def announce_stale_workers(
    *,
    threshold: float = STALENESS_THRESHOLD_SECONDS,
    now: float | None = None,
) -> list[str]:
    """Log one WARNING per newly stale worker; return every stale id.

    Pure disk reads and logging — no transitions, no writes. Safe to call on
    every kernel wake.
    """
    current = now if now is not None else time.time()
    stale: list[str] = []
    live_ids: set[str] = set()
    for record in list_active_records():
        if record.status in TERMINAL_STATUSES:
            continue
        live_ids.add(record.id)
        if not is_heartbeat_stale(record.id, now=current, threshold=threshold):
            continue
        stale.append(record.id)
        if record.id in _announced:
            continue
        _announced.add(record.id)
        mtime = read_heartbeat_mtime(record.id)
        silent_for = "never beat" if mtime is None else f"{current - mtime:.0f}s ago"
        log.warning(
            "worker %s (%s) is %s but its heartbeat last moved %s — "
            "it is not doing the work it was given",
            record.id,
            record.kind.value,
            record.status.value,
            silent_for,
        )

    # Forget workers that are no longer live, so this set cannot grow without
    # bound. It must filter by STATUS, not by presence in `active/`:
    # `KernelWorkerRunner` writes a terminal status but never calls
    # `archive_record` (only cancel and recovery do), so a normally completed
    # worker sits in `active/` indefinitely and an unfiltered scan would keep
    # re-adding it forever.
    _announced.intersection_update(live_ids)
    return stale


__all__ = ["announce_stale_workers", "reset_announced_for_tests"]
