"""State-transition mapper for RecoveryManager.

Pure functions, no I/O. Maps ``(prior_status, signals)`` →
``(recovered_status, reason)`` per the table in
``_shared/recovery-state-machine.md §State transitions emitted``.

Recovery never emits ``abandoned`` — that belongs to the AU-6 Governor.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusTransition:
    """One transition entry — prior → recovered with a machine-readable
    reason. ``reason`` is matched against the canonical strings in
    `_shared/recovery-state-machine.md` so the UI can localize / theme."""

    prior: str
    recovered: str
    reason: str

    @property
    def changed(self) -> bool:
        return self.prior != self.recovered


# Reason strings — canonical (referenced by the dashboard renderer + tests).
REASON_WORKER_ALIVE = "worker_alive_after_restart"
REASON_WORKER_LOST = "worker_lost_at_restart"
REASON_PANE_LOST = "pane_lost_at_restart"
REASON_REQUEUED = "requeued_after_restart"
REASON_SPAWN_ABORTED = "spawn_aborted"
REASON_STALE_HEARTBEAT = "stale_heartbeat"
REASON_AGENDA_RESUME = "agenda_resume"
REASON_WORKER_INTERRUPTED_NO_RETRY = "worker_interrupted_no_retry"
REASON_PRESERVED = "preserved_through_restart"


def map_worker_transition(
    *,
    prior_status: str,
    heartbeat_fresh: bool,
    pid_alive: bool,
    has_blocking_ask: bool = False,
) -> StatusTransition:
    """Worker-record subset of the transition map.

    AU-2 S1 worker scan is partial — the durable worker substrate lands
    in AU-3. This helper is exported now so AU-3 can plug in without
    redefining the transition rules.
    """
    p = prior_status
    if p == "running":
        if heartbeat_fresh and pid_alive:
            return StatusTransition(p, p, REASON_WORKER_ALIVE)
        return StatusTransition(p, "interrupted", REASON_STALE_HEARTBEAT)
    if p == "queued":
        return StatusTransition(p, "resume_queued", REASON_REQUEUED)
    if p == "awaiting_io":
        if has_blocking_ask:
            return StatusTransition(p, p, REASON_PRESERVED)
        return StatusTransition(p, "interrupted", REASON_WORKER_LOST)
    if p == "spawning":
        return StatusTransition(p, "failed", REASON_SPAWN_ABORTED)
    return StatusTransition(p, p, REASON_PRESERVED)


__all__ = [
    "StatusTransition",
    "map_worker_transition",
    "REASON_WORKER_ALIVE",
    "REASON_WORKER_LOST",
    "REASON_PANE_LOST",
    "REASON_REQUEUED",
    "REASON_SPAWN_ABORTED",
    "REASON_STALE_HEARTBEAT",
    "REASON_AGENDA_RESUME",
    "REASON_WORKER_INTERRUPTED_NO_RETRY",
    "REASON_PRESERVED",
]
