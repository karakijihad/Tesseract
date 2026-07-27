"""AU-2 — RecoveryManager: boot-time reconciler.

Runs once per backend boot, BEFORE scheduler catch-up and BEFORE
AutonomyKernel resumes (AU-5). Converts ambiguous post-restart state
into explicit, operator-visible outcomes by scanning durable state and
applying the transition map in ``_shared/recovery-state-machine.md``.

Public surface:
- :class:`RecoveryManager` — orchestrates the scans.
- :class:`RecoverySummary` — the per-boot workspace event payload.
"""

from tesseract.orchestrator.recovery.manager import (
    RecoveryManager,
    boot_id,
    new_recovery_manager,
)
from tesseract.orchestrator.recovery.summary import (
    RecoverySummary,
    build_recovery_event,
    empty_scan_counts,
)
from tesseract.orchestrator.recovery.transitions import (
    StatusTransition,
)

__all__ = [
    "RecoveryManager",
    "RecoverySummary",
    "StatusTransition",
    "boot_id",
    "build_recovery_event",
    "empty_scan_counts",
    "new_recovery_manager",
]
