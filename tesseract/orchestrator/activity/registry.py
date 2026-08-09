"""AS-1 — the Unified Activity Registry.

A process-global, in-memory index of every running unit of the assistant's work.
Each substrate (delegate / lane / controller session / …) reports in via
best-effort hooks; every mutation publishes an ``activity`` event so the
Mirror can reflect the live set. The registry owns NO persistent store —
on a restart the persistent items (lanes, sessions) are re-indexed from
their canonical on-disk files via ``rebuild_from_disk`` (AS-1 Phase 6);
ephemeral items (delegates) simply vanish, which is correct.

Known limitation (2026-07-05): a FAILED routine or autonomy run transitions
to a ``failed`` state instead of being removed (``hooks.py::fail_routine`` /
``fail_autonomy``) so the operator doesn't miss it, and it stays until
dismissed via ``POST /api/activity/{id}/close``. Because the registry is
in-memory only, a failed chip does NOT survive a backend restart — this is
deliberately not solved with persistence here. The durable record of the
failure remains in the substrate's own log: ``tesseract/logs/schedule/runs.jsonl``
for routines, the autonomy operator journal for workers.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace

from tesseract.orchestrator.activity.events import publish_activity_event
from tesseract.orchestrator.activity.models import (
    ActivityRecord,
    ActivityRecordOut,
    ActivityState,
    utc_now_iso,
)

log = logging.getLogger(__name__)

# AS-1 gap-c: terminal ephemeral records (finished delegates) have no owner to
# remove them and would accumulate in the process-global registry until restart.
# Bound by COUNT (not a time-TTL — avoids a hardcoded infra timeout): keep the
# newest N finished ephemeral records for recent-history display, evict older.
# Persistent items (lanes/sessions) are never swept — they mirror durable state.
_MAX_TERMINAL_EPHEMERAL = 50
_TERMINAL_STATES = frozenset({"done", "failed", "cancelled", "closed"})


class ActivityRegistry:
    """In-memory index keyed by ``activity_id``. The lock guards the dict;
    every event is published OUTSIDE the lock (the bus is loop-thread-only,
    so publishing under a lock held across an await-free call is safe, but
    we keep the critical section minimal)."""

    def __init__(self) -> None:
        self._records: dict[str, ActivityRecord] = {}
        self._lock = threading.Lock()

    def register(self, record: ActivityRecord, *, publish: bool = True) -> None:
        """Upsert a record. Preserves the original ``started_at`` on re-register
        and stamps ``updated_at``. Emits ``activity_registered`` for a new id,
        ``activity_updated`` for an existing one.

        ``publish=False`` seeds the dict WITHOUT touching the (loop-thread-only)
        bus — used by ``rebuild_from_disk`` at boot, which runs off the event
        loop via ``asyncio.to_thread``. No event is needed there: the seed
        happens before any subscriber connects, and REST ``GET /api/activity``
        is the hydration path (the WS pump deliberately drops replay)."""
        now = utc_now_iso()
        with self._lock:
            existing = self._records.get(record.activity_id)
            started = record.started_at or (existing.started_at if existing else now)
            rec = replace(record, started_at=started, updated_at=now)
            self._records[rec.activity_id] = rec
            evt = "activity_updated" if existing else "activity_registered"
        if publish:
            publish_activity_event(kind=evt, record=rec)
            # Opportunistic bound — only on the live (publish) path, never during
            # the boot rebuild (publish=False), which seeds persistent items only.
            self.sweep_terminal_ephemeral()

    def sweep_terminal_ephemeral(
        self, max_keep: int = _MAX_TERMINAL_EPHEMERAL
    ) -> list[ActivityRecord]:
        """Evict the oldest terminal ephemeral records beyond ``max_keep``,
        bounding registry growth between restarts (AS-1 gap-c). Returns the
        evicted records (each also emits ``activity_removed``). ``updated_at``
        is ISO-8601, so a lexicographic sort is chronological."""
        with self._lock:
            terminal = [
                r
                for r in self._records.values()
                if r.durability == "ephemeral" and r.state in _TERMINAL_STATES
            ]
            if len(terminal) <= max_keep:
                return []
            terminal.sort(key=lambda r: r.updated_at)
            evicted = terminal[: len(terminal) - max_keep]
            for r in evicted:
                self._records.pop(r.activity_id, None)
        for r in evicted:
            publish_activity_event(kind="activity_removed", record=r)
        return evicted

    def update_state(
        self, activity_id: str, state: ActivityState, *, result: str | None = None
    ) -> None:
        """Transition an existing record's state. No-op (not an error) for an
        unknown id — a substrate may emit a close for a record never seen.

        ``result`` optionally stamps the terminal outcome summary (e.g. a
        failure's short error detail) — omitted callers leave the field
        untouched."""
        with self._lock:
            existing = self._records.get(activity_id)
            if existing is None:
                return
            changes: dict = {"state": state, "updated_at": utc_now_iso()}
            if result is not None:
                changes["result"] = result
            rec = replace(existing, **changes)
            self._records[activity_id] = rec
        publish_activity_event(kind="activity_updated", record=rec)

    def remove(self, activity_id: str) -> None:
        with self._lock:
            rec = self._records.pop(activity_id, None)
        if rec is not None:
            publish_activity_event(kind="activity_removed", record=rec)

    def get(self, activity_id: str) -> ActivityRecord | None:
        with self._lock:
            return self._records.get(activity_id)

    def snapshot(self) -> list[ActivityRecordOut]:
        """Full current set as wire models — the REST hydration payload."""
        with self._lock:
            records = list(self._records.values())
        return [ActivityRecordOut.from_record(r) for r in records]


_registry: ActivityRegistry | None = None


def get_activity_registry() -> ActivityRegistry:
    """Process-wide singleton (same pattern as ``get_background_bus``)."""
    global _registry
    if _registry is None:
        _registry = ActivityRegistry()
    return _registry


def reset_activity_registry() -> None:
    """Test-only: drop the singleton so each test starts clean."""
    global _registry
    _registry = None


__all__ = ["ActivityRegistry", "get_activity_registry", "reset_activity_registry"]
