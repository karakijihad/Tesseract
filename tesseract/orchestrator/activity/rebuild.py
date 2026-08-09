"""AS-1 Phase 6 — re-index disk-durable substrates into the activity registry.

Runs at boot in BOTH processes that hold an Activity registry:
- the **Mirror** process (before the live push subscriber connects), so the
  registry reflects already-running persistent work; and
- the **controller** daemon (``daemon.py::_seed_activity_registry``, AS-1 gap-b),
  so its own registry isn't empty after a restart — otherwise live lane
  ``update_lane_state`` transitions are dropped and named lanes show under their
  bare id until the next ``ensure``.
Seeded from the canonical on-disk files (``lane.json`` / ``named-lanes/*.json`` /
``agent_controller/sessions/*.json``) with ``publish=False``. Live
``running``/``idle`` transitions then layer on (and, Mirror-side, arrive via the
controller→Mirror push subscriber).

Ephemeral delegates are deliberately NOT rebuilt: they died with the
process that spawned them, so re-indexing them would resurrect ghosts.

Kept OUT of ``registry.py`` so the registry stays substrate-agnostic (no
``agent_controller`` import) and there is no import-direction coupling.
Every disk read is best-effort per item — one unreadable record never
aborts the rest of the rebuild.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from tesseract.orchestrator.activity.hooks import LANE_STATE, SESSION_STATE
from tesseract.orchestrator.activity.models import ActivityRecord
from tesseract.orchestrator.activity.registry import (
    ActivityRegistry,
    get_activity_registry,
)

log = logging.getLogger(__name__)

# Lifecycles/statuses that mean "no longer running" — skipped at rebuild so
# the registry seeds only actually-live work (closed lanes are already
# archived out of ``list_lane_ids``; closed sessions still have records).
# ``closing`` is transitionally dead — its close→removed push may already have
# fired (or the lane is mid-archive), so seeding it would create a phantom
# that never self-cleans.
_DEAD_LANE_LIFECYCLES = {"closing", "closed", "error"}
_DEAD_SESSION_STATUSES = {"closed"}


def rebuild_from_disk(registry: ActivityRegistry | None = None) -> int:
    """Re-index named lanes, bare lanes, and controller sessions from disk.
    Returns the number of records registered. Never raises — disk/parse
    failures are logged and skipped."""
    reg = registry or get_activity_registry()
    return (
        _rebuild_named_lanes(reg)
        + _rebuild_bare_lanes(reg)
        + _rebuild_sessions(reg)
    )


def _rebuild_named_lanes(reg: ActivityRegistry) -> int:
    try:
        from tesseract.orchestrator.agent_controller.lanes.named import (
            list_named_lanes,
        )
        from tesseract.orchestrator.agent_controller.lanes.store import read_lane
    except Exception:  # noqa: BLE001
        log.warning("activity rebuild: named-lane imports failed", exc_info=True)
        return 0
    count = 0
    for record in list_named_lanes():
        try:
            lane = read_lane(record.lane_id)
        except Exception:  # noqa: BLE001 — orphan binding (lane archived/gone)
            continue
        if lane.lifecycle in _DEAD_LANE_LIFECYCLES:
            continue
        try:
            reg.register(
                ActivityRecord(
                    activity_id=f"lane:{record.lane_id}",
                    kind="lane",
                    label=record.name,
                    state=LANE_STATE.get(lane.lifecycle, "idle"),  # type: ignore[arg-type]
                    durability="persistent",
                    provider=record.kind,
                    transcript_ref=f"controller/lanes/{record.lane_id}/transcript.txt",
                    owner_principal=lane.owner_principal,
                    shared_with=tuple(lane.shared_with),
                ),
                publish=False,
            )
            count += 1
        except Exception:  # noqa: BLE001
            log.warning(
                "activity rebuild: named lane %s failed", record.name, exc_info=True
            )
    return count


def _rebuild_bare_lanes(reg: ActivityRegistry) -> int:
    """Lanes opened directly (not via a named binding). Skips any id a named
    binding already registered so the named label/provider is not clobbered."""
    try:
        from tesseract.orchestrator.agent_controller.lanes.store import (
            list_lane_ids,
            read_lane,
        )
    except Exception:  # noqa: BLE001
        log.warning("activity rebuild: lane-store imports failed", exc_info=True)
        return 0
    count = 0
    for lane_id in list_lane_ids():
        if reg.get(f"lane:{lane_id}") is not None:
            continue
        try:
            lane = read_lane(lane_id)
        except Exception:  # noqa: BLE001
            continue
        if lane.lifecycle in _DEAD_LANE_LIFECYCLES:
            continue
        try:
            reg.register(
                ActivityRecord(
                    activity_id=f"lane:{lane_id}",
                    kind="lane",
                    label=lane_id,
                    state=LANE_STATE.get(lane.lifecycle, "idle"),  # type: ignore[arg-type]
                    durability="persistent",
                    provider=lane.kind,
                    transcript_ref=f"controller/lanes/{lane_id}/transcript.txt",
                    owner_principal=lane.owner_principal,
                    shared_with=tuple(lane.shared_with),
                ),
                publish=False,
            )
            count += 1
        except Exception:  # noqa: BLE001
            log.warning(
                "activity rebuild: bare lane %s failed", lane_id, exc_info=True
            )
    return count


def _session_recency_cutoff() -> "datetime | None":
    """UTC cutoff older-than-which sessions are not re-registered, or None
    when the config can't be read (fail-open to the unfiltered pre-2026-07-03
    behavior — rebuild must never abort boot; the error is logged loudly)."""
    try:
        from tesseract.config.cockpit import load_activity_rebuild_window_hours

        window_h = load_activity_rebuild_window_hours()
        return datetime.now(timezone.utc) - timedelta(hours=window_h)
    except Exception:  # noqa: BLE001
        log.exception(
            "activity rebuild: cockpit.yaml activity.rebuild_session_window_hours "
            "unreadable — seeding ALL non-closed sessions"
        )
        return None


def _session_is_stale(record: object, cutoff: "datetime | None") -> bool:
    """True when the session's last_active_at predates the cutoff. Malformed
    or missing timestamps keep the session (best-effort contract)."""
    if cutoff is None:
        return False
    raw = getattr(record, "last_active_at", None)
    if not raw:
        return False
    try:
        last_active = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if last_active.tzinfo is None:
        last_active = last_active.replace(tzinfo=timezone.utc)
    return last_active < cutoff


def _rebuild_sessions(reg: ActivityRegistry) -> int:
    try:
        from tesseract.orchestrator.agent_controller.sessions import SessionRegistry
    except Exception:  # noqa: BLE001
        log.warning("activity rebuild: session imports failed", exc_info=True)
        return 0
    count = 0
    try:
        records = SessionRegistry().list_sessions()
    except Exception:  # noqa: BLE001
        log.warning("activity rebuild: list_sessions failed", exc_info=True)
        return 0
    cutoff = _session_recency_cutoff()
    skipped_stale = 0
    for record in records:
        if record.status in _DEAD_SESSION_STATUSES:
            continue
        if _session_is_stale(record, cutoff):
            # Sessions idle past the window stay on disk (transcripts
            # untouched) but don't flood the routing map — 382 idle rows
            # observed 2026-07-03.
            skipped_stale += 1
            continue
        try:
            reg.register(
                ActivityRecord(
                    activity_id=f"session:{record.session_id}",
                    kind="controller_session",
                    label=record.title or record.mode,
                    state=SESSION_STATE.get(record.status, "idle"),  # type: ignore[arg-type]
                    durability="persistent",
                    transcript_ref=record.transcript_path,
                    owner_principal=record.owner_principal,
                ),
                publish=False,
            )
            count += 1
        except Exception:  # noqa: BLE001
            log.warning(
                "activity rebuild: session %s failed",
                record.session_id,
                exc_info=True,
            )
    if skipped_stale:
        log.info(
            "activity rebuild: skipped %d session(s) idle past the recency window",
            skipped_stale,
        )
    return count


__all__ = ["rebuild_from_disk"]
