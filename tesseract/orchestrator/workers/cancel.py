"""Cancellation protocol for durable workers.

Each ``WorkerKind`` registers a kind-specific canceller. The dispatch
flow (per ``_shared/worker-record-schema.md §Cancellation protocol``):

1. Caller invokes ``cancel_worker(worker_id, reason=...)``.
2. Module loads the record from disk, looks up the registered
   canceller for that kind, and ``await``s it.
3. Record is transitioned to ``cancelled`` (with the reason and the
   canceller's outcome appended to ``error_message`` on failure),
   written atomically, and archived.

AU-3 S1 ships the protocol + record-side state machine. The concrete
cancellers (asyncio Task ref, chat-session ``cancel_turn``, ...) are
injected at boot by AU-5's AutonomyKernel — the kernel holds the live
references, so loose coupling here. Tests inject fakes via
``register_canceller``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, runtime_checkable

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    WorkerRecord,
    WorkerStatus,
    archive_record,
    load_record,
    write_record,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CancelOutcome:
    """One cancellation result. ``cancelled=True`` means the underlying
    process/task is no longer running (either gracefully terminated or
    force-killed). ``cancelled=False`` means the canceller could not
    confirm termination — the worker is left in ``cancelled`` status on
    disk anyway, but ``detail`` records the issue for the operator."""

    cancelled: bool
    detail: str = ""


@runtime_checkable
class WorkerCanceller(Protocol):
    """One canceller per ``WorkerKind``. Implementations:

    - ``tars_self``: ``asyncio.Task.cancel()`` then ``await`` the task.
    - ``markdown_agent``: ``ChatSession.cancel_turn()``.
    - ``claude_cli`` / ``codex_cli`` / ``terminal``: no canceller is
      registered for these kinds — a cancel request records intent
      (state still transitions to ``cancelled``) but the underlying
      process is not force-killed.
    """

    async def __call__(self, record: WorkerRecord) -> CancelOutcome: ...


CancellerFn = Callable[[WorkerRecord], Awaitable[CancelOutcome]]


_REGISTRY: dict[WorkerKind, CancellerFn] = {}


def register_canceller(kind: WorkerKind, canceller: CancellerFn) -> None:
    """AU-5 calls this once per kind at boot. Subsequent calls overwrite
    — tests register fakes per-test. The registry is module-level so
    the kernel and tools share one truth source."""
    _REGISTRY[kind] = canceller


def unregister_canceller(kind: WorkerKind) -> None:
    """Clean-up hook for tests; production calls boot-time registration
    once and never unregisters."""
    _REGISTRY.pop(kind, None)


def clear_registry() -> None:
    """Test-only: empty the registry. Production code does not need
    this — boot-time registration is permanent."""
    _REGISTRY.clear()


async def cancel_worker(worker_id: str, *, reason: str = "operator_cancelled") -> CancelOutcome:
    """Look up the record, dispatch to the registered canceller, write
    the cancelled record back, and archive it.

    Failure modes:

    - Record missing → ``CancelOutcome(cancelled=False, detail="record_missing")``.
    - Record already terminal → ``CancelOutcome(cancelled=False, detail="already_terminal")``.
    - No canceller registered for the kind → record IS still transitioned
      to ``cancelled`` (so the dashboard reflects operator intent) but
      the outcome reports ``no_canceller`` so the operator knows the
      underlying process may still be alive.
    - Canceller raises → record IS still transitioned to ``cancelled``;
      the exception class lands in ``error_class`` / ``error_message``.
    """
    record = load_record(worker_id)
    if record is None:
        return CancelOutcome(cancelled=False, detail="record_missing")
    if record.is_terminal():
        return CancelOutcome(cancelled=False, detail=f"already_terminal:{record.status.value}")

    canceller = _REGISTRY.get(record.kind)
    outcome: CancelOutcome
    if canceller is None:
        outcome = CancelOutcome(cancelled=False, detail="no_canceller_registered")
        log.warning("cancel_worker %s: no canceller for kind %s", worker_id, record.kind.value)
    else:
        try:
            outcome = await canceller(record)
        except Exception as exc:  # noqa: BLE001 — record must always land cancelled
            log.exception("cancel_worker %s: canceller raised", worker_id)
            record.error_class = type(exc).__name__
            record.error_message = str(exc)[:2000]
            outcome = CancelOutcome(cancelled=False, detail=f"canceller_raised:{type(exc).__name__}")

    record.transition_to(WorkerStatus.CANCELLED, reason=reason)
    if not outcome.cancelled and outcome.detail and not record.error_message:
        record.error_message = outcome.detail
    write_record(record)
    archive_record(record)
    return outcome


__all__ = [
    "CancelOutcome",
    "CancellerFn",
    "WorkerCanceller",
    "cancel_worker",
    "clear_registry",
    "register_canceller",
    "unregister_canceller",
]
