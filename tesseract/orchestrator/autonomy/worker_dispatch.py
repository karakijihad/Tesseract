"""Worker dispatch — kernel-side helper that turns a selected
:class:`AgendaItem` into a durable :class:`WorkerRecord` and hands it
to an injectable :class:`WorkerRunner`.

Per GOVERNANCE §6 "Restart-safe end-to-end": the durable record MUST
land on disk *before* the runner starts. If the runner crashes or the
backend exits between record-write and process-start, the next boot's
RecoveryManager (AU-2 scan 2) classifies the record as ``interrupted``
and the retry policy decides whether to resume.

The runner itself is a :class:`Protocol` so test fixtures can pass
an in-memory mock (immediate complete, deterministic exit, etc.)
without spinning up subprocesses. Production wiring will provide a
concrete runner per :class:`WorkerKind`; that lands when AU-5 wires
real per-kind runners — the kernel only needs the contract.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol

from tesseract.orchestrator.autonomy.models import AgendaItem
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    RiskClass as WorkerRiskClass,
    WorkerRecord,
    WorkerStatus,
    mint_worker_id,
    write_record,
)

log = logging.getLogger(__name__)


class WorkerRunner(Protocol):
    """Concrete runner contract. The kernel writes the record + admits
    to the lane, THEN hands the record to the runner. The runner is
    responsible for the actual process spawn / asyncio work and for
    writing back terminal state via ``write_record``.

    ``run`` is fire-and-forget from the kernel's view — the kernel
    awaits the coroutine in a background task so a stuck runner does
    not block the tick loop. The runner contract is: return when the
    worker is fully terminated AND its record is persisted."""

    async def run(self, record: WorkerRecord) -> None: ...  # pragma: no cover


# Default runner the kernel falls back to when none is injected — which
# on the Mirror happens whenever the tool registry is unavailable at
# boot. It runs nothing, so it refuses: the record lands on disk and the
# agenda item is parked for the operator rather than closed.
#
# It used to mark the worker DONE, and reconciliation then closed the
# item as completed work. On a boot with no registry that made every
# selected item read as finished, having done nothing at all — the same
# empty-is-success defect the result vocabulary exists to remove, in the
# one place where nothing whatsoever ran.
class _NoopRunner:
    """Default WorkerRunner. Records the dispatch and refuses it.

    Production replaces this with per-kind runners. The record still
    proves the dispatch path end to end: WorkerRecord on disk, agenda
    item linked, the worker terminal — as a refusal, not a completion.
    """

    async def run(self, record: WorkerRecord) -> None:
        record.set_outcome(
            RunOutcome.REFUSED,
            reason="no worker runner is wired in this process, so nothing ran",
        )
        record.summary = "no runner wired — the dispatch was recorded, not performed"
        record.transition_to(
            WorkerStatus.BLOCKED,
            reason="no_runner_wired",
        )
        write_record(record)


def default_runner() -> WorkerRunner:
    return _NoopRunner()


def build_worker_record(
    item: AgendaItem,
    *,
    kind: WorkerKind,
    role: str,
    now: datetime | None = None,
) -> WorkerRecord:
    """Mint a :class:`WorkerRecord` linked to ``item``. Persistence
    is the caller's job — this is a pure factory."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return WorkerRecord(
        id=mint_worker_id(kind, now=moment),
        kind=kind,
        created_at=moment,
        updated_at=moment,
        agenda_item_id=item.id,
        risk_class=_to_worker_risk(item.risk_class),
        role=role,
        prompt=item.goal,
        status=WorkerStatus.QUEUED,
    )


def _to_worker_risk(item_class: WorkerRiskClass) -> WorkerRiskClass:
    """The worker substrate uses the same :class:`RiskClass` enum as
    the agenda layer (re-exported); identity mapping today."""
    return item_class


__all__ = [
    "WorkerRunner",
    "build_worker_record",
    "default_runner",
]
