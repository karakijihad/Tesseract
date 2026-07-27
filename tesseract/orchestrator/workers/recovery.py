"""Per-kind recovery handlers for the durable worker substrate (AU-3 S2).

Wired into ``RecoveryManager.scan_workers`` (AU-2 scan 2 expansion). On
boot, the manager walks ``<TESSERACT_HOME>/workers/active/`` via the
count-only summary, then for each non-terminal record asks the
registered handler to classify and persist.

Per ``_shared/worker-record-schema.md §Recovery handler``:

- ``tars_self``: never resumable (in-process asyncio state is lost).
  ``mark_interrupted`` always.
- ``markdown_agent``: resumable if the agent's transcript is complete;
  else ``interrupted``. AU-3 S2 ships the conservative variant — every
  markdown agent is marked interrupted; AU-5 lands the transcript-
  completeness probe.
- ``claude_cli`` / ``codex_cli``: PTY pane is gone after backend
  restart by construction. ``mark_interrupted`` with the transcript
  path pointer preserved so the kernel can decide retry vs escalate.
- ``terminal``: ``mark_interrupted`` always; operator decides.

The handler API is intentionally narrow: ``can_recover``/``resume``/
``mark_interrupted``. AU-5 may inject richer handlers (with live
process references) at boot; the module-level registry follows the
same pattern as ``cancel.py``.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.recovery.transitions import (
    REASON_PANE_LOST,
    REASON_STALE_HEARTBEAT,
    REASON_WORKER_LOST,
)
from tesseract.orchestrator.workers.heartbeat import is_heartbeat_stale
from tesseract.orchestrator.workers.record import (
    WorkerRecord,
    WorkerStatus,
    archive_record,
    load_record,
    write_record,
)

log = logging.getLogger(__name__)


@runtime_checkable
class WorkerRecovery(Protocol):
    """Per-kind recovery handler."""

    def can_recover(self, record: WorkerRecord) -> bool: ...

    async def resume(self, record: WorkerRecord) -> WorkerRecord: ...

    async def mark_interrupted(self, record: WorkerRecord, reason: str) -> WorkerRecord: ...


# Default handlers — all conservative, all ``can_recover -> False`` in
# AU-3 S2. AU-5 will register richer handlers that actually attempt
# resume where state permits.


class _InterruptOnlyHandler:
    """Default handler for every kind in S2. Resume support lands in
    AU-5 once the AutonomyKernel knows how to rehydrate a worker."""

    def __init__(self, kind: WorkerKind) -> None:
        self._kind = kind

    def can_recover(self, record: WorkerRecord) -> bool:  # noqa: ARG002
        return False

    async def resume(self, record: WorkerRecord) -> WorkerRecord:
        raise NotImplementedError(
            f"resume not supported for {self._kind.value} in AU-3 S2"
        )

    async def mark_interrupted(self, record: WorkerRecord, reason: str) -> WorkerRecord:
        if record.is_terminal():
            return record
        record.transition_to(WorkerStatus.INTERRUPTED, reason=reason)
        write_record(record)
        archive_record(record)
        # Lazy import to avoid a workers/ → autonomy/ layering reversal.
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


_REGISTRY: dict[WorkerKind, WorkerRecovery] = {
    kind: _InterruptOnlyHandler(kind) for kind in WorkerKind
}


def register_recovery_handler(kind: WorkerKind, handler: WorkerRecovery) -> None:
    """AU-5 calls this at boot to override the conservative default
    with a richer handler. Tests register fakes per-test."""
    _REGISTRY[kind] = handler


def get_recovery_handler(kind: WorkerKind) -> WorkerRecovery:
    return _REGISTRY[kind]


def reset_recovery_handlers() -> None:
    """Test-only: reset to the conservative defaults so test cases
    don't leak handlers across each other."""
    _REGISTRY.clear()
    for kind in WorkerKind:
        _REGISTRY[kind] = _InterruptOnlyHandler(kind)


def classify_recovery_reason(record: WorkerRecord) -> str:
    """Pick the canonical recovery reason for a non-terminal record on
    boot. Order: stale heartbeat → PTY-bound (claude_cli / terminal) →
    generic worker-lost. Reasons are the same strings
    ``recovery/transitions.py`` already exports so the dashboard
    renderer renders them consistently across mission + worker rows.

    TC-3 (2026-05-23) — CODEX_CLI was removed from the PTY set because
    codex never ran in a PTY; the worker routes through the controller
    dispatcher (``dispatch_to_controller``), not a backend-owned pane. A
    lost codex worker is REASON_WORKER_LOST, not REASON_PANE_LOST.

    TC-4 (2026-05-23) — TARS_CONTROLLER never appears in the PTY set:
    the controller runs as a sibling OS process speaking loopback TCP,
    not as a backend-owned PTY pane. The dedicated
    ``_TarsControllerRecoveryHandler.can_recover`` gate runs FIRST in
    ``recover_worker``; only if that returns False do we fall through
    here, and the right reason is then REASON_WORKER_LOST."""
    if is_heartbeat_stale(record.id):
        return REASON_STALE_HEARTBEAT
    pty_kinds = {WorkerKind.CLAUDE_CLI, WorkerKind.TERMINAL}
    if record.kind in pty_kinds:
        return REASON_PANE_LOST
    return REASON_WORKER_LOST


def is_pid_alive(pid: int | None) -> bool:
    """Cross-platform liveness probe routed through
    :func:`tesseract.supervisor.process_probe.pid_alive`. The earlier
    POSIX-style ``os.kill(pid, 0)`` implementation misclassified live
    Windows processes as dead (WinError 87) — AU-3 recovery now uses
    the proper ``OpenProcess`` + ``GetExitCodeProcess`` probe on
    Windows. Fail-safe: any unexpected error is treated as 'not
    alive', so recovery transitions to ``interrupted`` rather than
    falsely-recovered."""
    from tesseract.supervisor.process_probe import pid_alive
    return pid_alive(pid)


async def recover_worker(worker_id: str) -> WorkerRecord | None:
    """Look up the record, decide recovery action, persist. Returns the
    updated record or ``None`` if the record is missing/malformed.

    Convention: non-terminal records on boot are interrupted by
    default in AU-3 S2 (no kind supports resume yet). The handler
    interface keeps the door open for AU-5 to plug in resume.
    """
    record = load_record(worker_id)
    if record is None:
        return None
    if record.is_terminal():
        return record
    handler = get_recovery_handler(record.kind)
    reason = classify_recovery_reason(record)
    if handler.can_recover(record):
        try:
            return await handler.resume(record)
        except Exception:  # noqa: BLE001 — fall back to interrupted
            log.exception("worker recovery: resume failed for %s; marking interrupted", worker_id)
    return await handler.mark_interrupted(record, reason)


def recover_worker_sync(worker_id: str) -> WorkerRecord | None:
    """Synchronous entry point for RecoveryManager's scan loop.

    The handler protocol is async per the schema contract (AU-5 may
    register handlers that await real IO during resume). In S2 every
    concrete handler is conservative — ``mark_interrupted`` does sync
    IO only, no awaits — so the scan can run it via ``asyncio.run``
    cleanly without nested-loop hazards. If we're already inside a
    running loop, fall back to driving the coroutine on a dedicated
    thread with a fresh loop; this only matters for unit-test
    pytest-asyncio harnesses that mark the scan test async.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
        nested = True
    except RuntimeError:
        nested = False

    if not nested:
        return asyncio.run(recover_worker(worker_id))

    import concurrent.futures

    def _runner() -> WorkerRecord | None:
        return asyncio.run(recover_worker(worker_id))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_runner)
        return future.result()


__all__ = [
    "WorkerRecovery",
    "classify_recovery_reason",
    "get_recovery_handler",
    "is_pid_alive",
    "recover_worker",
    "recover_worker_sync",
    "register_recovery_handler",
    "reset_recovery_handlers",
]
