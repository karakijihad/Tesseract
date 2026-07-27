"""AU-2 — pure transition mapper tests.

Locks the transition table from `_shared/recovery-state-machine.md
§State transitions emitted`. No I/O.
"""

from __future__ import annotations

from tesseract.orchestrator.recovery.transitions import (
    REASON_STALE_HEARTBEAT,
    map_worker_transition,
)


def test_worker_running_with_fresh_heartbeat_preserved() -> None:
    tr = map_worker_transition(prior_status="running", heartbeat_fresh=True, pid_alive=True)
    assert tr.recovered == "running"


def test_worker_running_stale_heartbeat_interrupted() -> None:
    tr = map_worker_transition(prior_status="running", heartbeat_fresh=False, pid_alive=True)
    assert tr.recovered == "interrupted"
    assert tr.reason == REASON_STALE_HEARTBEAT


def test_worker_spawning_fails_as_spawn_aborted() -> None:
    tr = map_worker_transition(prior_status="spawning", heartbeat_fresh=False, pid_alive=False)
    assert tr.recovered == "failed"
    assert tr.reason == "spawn_aborted"
