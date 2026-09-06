"""TC-4 — `_AgentControllerRecoveryHandler` per `_shared/recovery-contract.md`.

Registered into `tesseract.orchestrator.workers.recovery` at controller
boot. `can_recover` returns True iff the WorkerRecord carries a
controller PID that is still alive AND the controller's heartbeat was
touched within `STALENESS_THRESHOLD_SECONDS` of TIME THE MACHINE WAS
RUNNING. The sleep and the shutdowns inside that age are subtracted from
it, because a heartbeat is only touched while the computer is on: after a
night asleep every heartbeat here is hours old, and the rule that read the
raw age was measuring the night rather than the worker.

`resume` does not respawn the controller — it cannot — it only transitions
the worker record from INTERRUPTED-candidate back to RUNNING and persists
the change. The IPC reattach itself is performed lazily on the next
controller call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from pathlib import Path

from tesseract.orchestrator.workers.heartbeat import STALENESS_THRESHOLD_SECONDS
from tesseract.orchestrator.workers.record import (
    WorkerRecord,
    WorkerStatus,
    archive_record,
    write_record,
)

log = logging.getLogger(__name__)


class AgentControllerRecoveryHandler:
    """Reattach-if-fresh handler for `WorkerKind.AGENT_CONTROLLER`."""

    def can_recover(self, record: WorkerRecord) -> bool:
        if record.controller_pid is None:
            return False
        from tesseract.orchestrator.workers.recovery import is_pid_alive

        if not is_pid_alive(record.controller_pid):
            return False
        if not record.controller_hb_path:
            return False
        hb = Path(record.controller_hb_path)
        if not hb.exists():
            return False
        # ONE clock reading, used for both ends. The age and the correction
        # were sampled separately at first, which is two clocks deciding one
        # thing in the change whose whole subject is that.
        now = datetime.now(timezone.utc)
        try:
            age = now.timestamp() - hb.stat().st_mtime
        except OSError:
            return False
        away = self._machine_was_away_for(age, now=now)
        return age - away <= STALENESS_THRESHOLD_SECONDS

    @staticmethod
    def _machine_was_away_for(age_seconds: float, *, now: datetime) -> float:
        """How much of the last `age_seconds` the computer was not running.

        Elapsed time never decides this alone, which is the whole point. A
        heartbeat is touched every thirty seconds by a process that only runs
        while the machine does, so after a night asleep every heartbeat on the
        machine is hours old and the old rule called every worker dead. It was
        measuring the night, not the worker.

        Windows keeps when it slept and when it was shut down, so the sleep is
        subtracted and what is left is time the worker could have used and did
        not. A record that cannot be read returns zero, which leaves the old
        rule exactly as it was: not knowing must not become an excuse, because
        the safe direction here is calling a dead worker dead.
        """
        from tesseract.orchestrator import machine_state

        try:
            # The window has to cover the AGE, not a fixed seven days. Reading
            # a fixed window was a regression this introduced: a heartbeat
            # older than it lost its correction entirely, because a gap is only
            # known once both its ends are in what was read, so a controller
            # genuinely asleep for over a week was marked interrupted rather
            # than reattached. That is the exact case this code exists for.
            known = _power_record(now, age_seconds)
            return machine_state.away_for(age_seconds, now, known=known)
        except Exception:  # noqa: BLE001 — a reading never decides a recovery
            log.debug("recovery: the machine's power record could not be read",
                      exc_info=True)
            return 0.0

    async def resume(self, record: WorkerRecord) -> WorkerRecord:
        if record.is_terminal():
            return record
        record.transition_to(
            WorkerStatus.RUNNING, reason="reattached_after_restart"
        )
        write_record(record)
        return record

    async def mark_interrupted(
        self, record: WorkerRecord, reason: str
    ) -> WorkerRecord:
        if record.is_terminal():
            return record
        record.transition_to(WorkerStatus.INTERRUPTED, reason=reason)
        write_record(record)
        archive_record(record)
        # Mirror the journal append from `_InterruptOnlyHandler` so the
        # operator timeline shows the controller-attested interruption.
        from tesseract.orchestrator.autonomy import journal as operator_journal

        operator_journal.append(
            "outcome",
            {
                "agenda_item_id": record.agenda_item_id or None,
                "worker_id": record.id,
                "status": record.status.value,
                "summary": reason,
                "artifacts": len(record.artifacts or []),
            },
        )
        return record


# The power record, read once and held for the length of a recovery pass.
#
# A boot with N interrupted workers asked the event log N times, each its own
# query, render and handle close, each bounded at five seconds. That cost is
# paid on the boot path and it scales with exactly the thing recovery exists
# to handle: a machine that came back with work outstanding.
#
# `machine_state.away_for` already takes the list rather than reading it, for
# this reason. The cache is keyed on nothing and cleared by age alone, because
# a recovery pass is seconds long and the machine cannot sleep during one.
_HELD: "tuple[datetime, datetime, list[Any] | None] | None" = None
_HOLD_FOR = timedelta(seconds=30)


def _power_record(now: datetime, age_seconds: float):
    """What the machine did, for every worker in one pass rather than each.

    Held with the WINDOW it was read for, not just the moment. A second worker
    whose heartbeat is older than the first one's needs a wider read, and a
    cache that answered it from a narrower one would hand back a record with
    the very absence that explains it missing.
    """
    global _HELD
    from tesseract.orchestrator import machine_state

    since = min(
        now - machine_state.LOOKBACK,
        now - timedelta(seconds=age_seconds + 60.0),
    )
    if _HELD is not None and now - _HELD[0] < _HOLD_FOR and _HELD[1] <= since:
        return _HELD[2]
    found = machine_state.gaps(since, now=now)
    _HELD = (now, since, found)
    return found


def forget_power_record() -> None:
    """Drop what is held. For tests, and for anything that wants the next
    reading to be fresh rather than up to `_HOLD_FOR` old."""
    global _HELD
    _HELD = None


def register_default_handler() -> None:
    """Bind the default handler. Called once at controller boot. Tests
    register their own fakes via `register_recovery_handler` directly."""
    from tesseract.orchestrator.workers.kinds import WorkerKind
    from tesseract.orchestrator.workers.recovery import register_recovery_handler

    register_recovery_handler(
        WorkerKind.AGENT_CONTROLLER, AgentControllerRecoveryHandler()
    )


__all__ = [
    "AgentControllerRecoveryHandler",
    "forget_power_record",
    "register_default_handler",
]
