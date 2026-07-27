"""Durable worker substrate for the AutonomyKernel (AU-3).

Parallels the per-session ``SpawnRegistry`` used for synchronous in-chat
helpers. Anything dispatched by AU-5's AutonomyKernel runs as a durable
worker — record on disk before work starts, heartbeat every 30s, lane-cap
admission, kind-specific cancellation, recovery handler registered with
RecoveryManager.

The substrate is intentionally additive: SpawnRegistry stays untouched
for ``delegate_claude`` / ``delegate_codex`` / ``invoke_agent`` called
from a chat turn. The durable substrate is the path autonomous work
takes.
"""

from tesseract.orchestrator.workers.cancel import (
    CancelOutcome,
    WorkerCanceller,
    cancel_worker,
    register_canceller,
)
from tesseract.orchestrator.workers.recovery import (
    WorkerRecovery,
    classify_recovery_reason,
    get_recovery_handler,
    is_pid_alive,
    recover_worker,
    register_recovery_handler,
)
from tesseract.orchestrator.workers.retry import (
    DEFAULT_RETRY,
    WorkerRetryDecision,
    WorkerRetryPolicy,
    WorkerRetryRule,
)
# Test-only helpers (`clear_registry`, `unregister_canceller`,
# `reset_recovery_handlers`) live on their submodules and are
# intentionally not re-exported here.
from tesseract.orchestrator.workers.heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    STALENESS_THRESHOLD_SECONDS,
    heartbeat_path,
    is_heartbeat_stale,
    read_heartbeat_mtime,
    touch_heartbeat,
)
from tesseract.orchestrator.workers.lane import (
    AdmissionDecision,
    AdmissionResult,
    WorkerLane,
)
from tesseract.orchestrator.workers.paths import (
    workers_active_dir,
    workers_archive_dir,
    worker_dir,
)
from tesseract.orchestrator.workers.record import (
    ArtifactRef,
    RiskClass,
    StatusTransition,
    WorkerRecord,
    WorkerStatus,
    archive_record,
    load_record,
    mint_worker_id,
    write_record,
)

__all__ = [
    "AdmissionDecision",
    "AdmissionResult",
    "ArtifactRef",
    "CancelOutcome",
    "DEFAULT_RETRY",
    "HEARTBEAT_INTERVAL_SECONDS",
    "RiskClass",
    "STALENESS_THRESHOLD_SECONDS",
    "StatusTransition",
    "WorkerCanceller",
    "WorkerLane",
    "WorkerRecord",
    "WorkerRecovery",
    "WorkerRetryDecision",
    "WorkerRetryPolicy",
    "WorkerRetryRule",
    "WorkerStatus",
    "archive_record",
    "cancel_worker",
    "classify_recovery_reason",
    "get_recovery_handler",
    "heartbeat_path",
    "is_heartbeat_stale",
    "is_pid_alive",
    "load_record",
    "mint_worker_id",
    "read_heartbeat_mtime",
    "recover_worker",
    "register_canceller",
    "register_recovery_handler",
    "touch_heartbeat",
    "worker_dir",
    "workers_active_dir",
    "workers_archive_dir",
    "write_record",
]
