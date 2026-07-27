"""TC-4 — `_TarsControllerRecoveryHandler` per `_shared/recovery-contract.md`.

Registered into `tesseract.orchestrator.workers.recovery` at controller
boot. `can_recover` returns True iff the WorkerRecord carries a
controller PID that is still alive AND the controller's heartbeat file is
fresh (mtime within `STALENESS_THRESHOLD_SECONDS`).

`resume` does not respawn the controller — it cannot — it only transitions
the worker record from INTERRUPTED-candidate back to RUNNING and persists
the change. The IPC reattach itself is performed lazily on the next
controller call.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from tesseract.orchestrator.workers.heartbeat import STALENESS_THRESHOLD_SECONDS
from tesseract.orchestrator.workers.record import (
    WorkerRecord,
    WorkerStatus,
    archive_record,
    write_record,
)

log = logging.getLogger(__name__)


class TarsControllerRecoveryHandler:
    """Reattach-if-fresh handler for `WorkerKind.TARS_CONTROLLER`."""

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
        try:
            age = time.time() - hb.stat().st_mtime
        except OSError:
            return False
        return age <= STALENESS_THRESHOLD_SECONDS

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


def register_default_handler() -> None:
    """Bind the default handler. Called once at controller boot. Tests
    register their own fakes via `register_recovery_handler` directly."""
    from tesseract.orchestrator.workers.kinds import WorkerKind
    from tesseract.orchestrator.workers.recovery import register_recovery_handler

    register_recovery_handler(
        WorkerKind.TARS_CONTROLLER, TarsControllerRecoveryHandler()
    )


__all__ = [
    "TarsControllerRecoveryHandler",
    "register_default_handler",
]
