"""AS-1 — best-effort substrate hooks into the Unified Activity Registry.

Substrates (lanes, controller sessions) call these one-liners to project
their lifecycle into the registry. Every call is wrapped so a registry /
bus failure NEVER propagates into the substrate — a reflection hiccup must
not break ``LaneManager.open`` / ``SessionRegistry.create_session`` etc.

In the controller daemon these hooks publish on the controller's bus; the
daemon's activity forwarder relays each event to connected Mirror clients
(see ``tars_controller/daemon.py``). All hook call sites run on the
controller event loop, so the loop-thread-only bus publish is safe without
``call_soon_threadsafe`` marshaling.

The lifecycle/status → :data:`ActivityState` maps live here as the single
source of truth, shared with ``rebuild.py``.
"""

from __future__ import annotations

import logging

from tesseract.orchestrator.activity.models import ActivityRecord
from tesseract.orchestrator.activity.registry import get_activity_registry

log = logging.getLogger(__name__)

# Lane lifecycle → normalized activity state. A ready/idle lane "exists but is
# not mid-turn" → ``idle``; only an in-flight turn (``busy``) is ``running``.
LANE_STATE = {
    "spawning": "spawning",
    "busy": "running",
    "ready": "idle",
    "idle": "idle",
    "closing": "closed",
    "closed": "closed",
    "error": "failed",
}

# Controller session status → normalized activity state.
SESSION_STATE = {
    "active": "running",
    "idle": "idle",
    "detached": "idle",
    "closed": "closed",
}


def register_lane(
    lane_id: str, *, label: str, provider: str | None, lifecycle: str = "ready"
) -> None:
    """Upsert a lane into the registry (durability=persistent)."""
    try:
        get_activity_registry().register(
            ActivityRecord(
                activity_id=f"lane:{lane_id}",
                kind="lane",
                label=label,
                state=LANE_STATE.get(lifecycle, "idle"),  # type: ignore[arg-type]
                durability="persistent",
                provider=provider,
                transcript_ref=f"controller/lanes/{lane_id}/transcript.txt",
            )
        )
    except Exception:  # noqa: BLE001 — reflection is best-effort
        log.warning("activity: register_lane %s failed", lane_id, exc_info=True)


def update_lane_state(lane_id: str, lifecycle: str) -> None:
    """Transition a lane's state from a lane *lifecycle* token (busy / ready /
    closed / error). No-op on an unknown id."""
    try:
        get_activity_registry().update_state(
            f"lane:{lane_id}", LANE_STATE.get(lifecycle, "idle")  # type: ignore[arg-type]
        )
    except Exception:  # noqa: BLE001
        log.warning("activity: update_lane_state %s failed", lane_id, exc_info=True)


def register_session(session_id: str, *, label: str, status: str) -> None:
    """Upsert a controller session into the registry (durability=persistent)."""
    try:
        get_activity_registry().register(
            ActivityRecord(
                activity_id=f"session:{session_id}",
                kind="controller_session",
                label=label,
                state=SESSION_STATE.get(status, "idle"),  # type: ignore[arg-type]
                durability="persistent",
            )
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "activity: register_session %s failed", session_id, exc_info=True
        )


def update_session_state(session_id: str, status: str) -> None:
    """Transition a controller session's state from a session *status* token.
    No-op on an unknown id."""
    try:
        get_activity_registry().update_state(
            f"session:{session_id}", SESSION_STATE.get(status, "idle")  # type: ignore[arg-type]
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "activity: update_session_state %s failed", session_id, exc_info=True
        )


def register_routine(run_id: str, *, label: str) -> None:
    """Upsert a running scheduled-job run (durability=ephemeral, live-only)."""
    try:
        get_activity_registry().register(
            ActivityRecord(
                activity_id=f"routine:{run_id}",
                kind="routine",
                label=label,
                state="running",
                durability="ephemeral",
            )
        )
    except Exception:  # noqa: BLE001 — reflection is best-effort
        log.warning("activity: register_routine %s failed", run_id, exc_info=True)


def remove_routine(run_id: str) -> None:
    """Drop a finished routine from the live registry (no-op on unknown id)."""
    try:
        get_activity_registry().remove(f"routine:{run_id}")
    except Exception:  # noqa: BLE001
        log.warning("activity: remove_routine %s failed", run_id, exc_info=True)


def fail_routine(run_id: str, *, detail: str) -> None:
    """Transition a routine to ``failed`` instead of removing it — the
    operator must not lose a failed run to a silent chip disappearance.
    The record stays in the registry until the operator dismisses it via
    ``POST /api/activity/{id}/close`` (no-op on unknown id)."""
    try:
        get_activity_registry().update_state(f"routine:{run_id}", "failed", result=detail)
    except Exception:  # noqa: BLE001
        log.warning("activity: fail_routine %s failed", run_id, exc_info=True)


def register_autonomy(item_id: str, *, label: str) -> None:
    """Upsert a running autonomy agenda-item (durability=ephemeral, live-only)."""
    try:
        get_activity_registry().register(
            ActivityRecord(
                activity_id=f"autonomy:{item_id}",
                kind="autonomy",
                label=label,
                state="running",
                durability="ephemeral",
            )
        )
    except Exception:  # noqa: BLE001
        log.warning("activity: register_autonomy %s failed", item_id, exc_info=True)


def remove_autonomy(item_id: str) -> None:
    """Drop a finished autonomy item (no-op on unknown id)."""
    try:
        get_activity_registry().remove(f"autonomy:{item_id}")
    except Exception:  # noqa: BLE001
        log.warning("activity: remove_autonomy %s failed", item_id, exc_info=True)


def fail_autonomy(item_id: str, *, detail: str) -> None:
    """Transition an autonomy item to ``failed`` instead of removing it —
    the operator must not lose a failed worker to a silent chip
    disappearance. The record stays in the registry until the operator
    dismisses it via ``POST /api/activity/{id}/close`` (no-op on unknown id)."""
    try:
        get_activity_registry().update_state(f"autonomy:{item_id}", "failed", result=detail)
    except Exception:  # noqa: BLE001
        log.warning("activity: fail_autonomy %s failed", item_id, exc_info=True)


__all__ = [
    "LANE_STATE",
    "SESSION_STATE",
    "register_lane",
    "update_lane_state",
    "register_session",
    "update_session_state",
    "register_routine",
    "remove_routine",
    "fail_routine",
    "register_autonomy",
    "remove_autonomy",
    "fail_autonomy",
]
