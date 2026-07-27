"""Cross-process WS notify for worker record mutations.

Phase 3 of the realtime-visibility rework. The autonomy kernel + governor
+ cancel/recovery paths each mutate worker records via module-level
``write_record`` + ``archive_record`` (no shared class instance to hook).
This module exposes a process-wide broadcaster:

  * ``set_worker_broadcast_hook(hook)`` — Mirror server boot wires this to
    a closure that schedules ``broadcast_worker_event(app, ...)`` via the
    running event loop. REPL / standalone-scheduler contexts leave it
    unset; broadcast then no-ops.
  * ``broadcast_worker_event(app, event_type, record)`` — fans
    ``worker_record_*`` envelopes to every Mirror WS session, mirroring
    the agenda broadcaster in shape and failure-safety.

Valid event types:
  - ``worker_record_started``     — first write of a fresh record (lane spawn)
  - ``worker_record_transitioned``— status change followed by write_record
  - ``worker_record_archived``    — terminal record moved to ``archive/``

The frontend ``stores/autonomy.ts`` already has switch branches for these
three types ready (AU-7 S1 — they were declared but never published).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from tesseract.orchestrator.workers.record import WorkerRecord

log = logging.getLogger(__name__)

WorkerBroadcastHook = Callable[[str, WorkerRecord], None]

_worker_broadcast_hook: WorkerBroadcastHook | None = None

_MIRROR_HELPERS: tuple[Any, Any] | None = None
_MIRROR_HELPERS_FAILED = False

VALID_EVENT_TYPES = frozenset(
    {"worker_record_started", "worker_record_transitioned", "worker_record_archived"}
)


def set_worker_broadcast_hook(hook: WorkerBroadcastHook | None) -> None:
    """Replace the process-wide worker broadcast hook. Mirror server boot
    wires this; REPL/test contexts leave it None so writes stay silent.
    """
    global _worker_broadcast_hook
    _worker_broadcast_hook = hook


def fire_worker_broadcast(event_type: str, record: WorkerRecord) -> None:
    """Synchronous fan-out invoked by ``record.write_record`` /
    ``record.archive_record``. Swallows every exception — broadcast
    failures must not affect the originating worker mutation.
    """
    if _worker_broadcast_hook is None:
        return
    if event_type not in VALID_EVENT_TYPES:
        log.warning("worker broadcast: unknown event_type %r", event_type)
        return
    try:
        _worker_broadcast_hook(event_type, record)
    except Exception:
        log.exception(
            "worker broadcast hook raised on %s for %s; non-fatal",
            event_type, record.id,
        )


def _load_mirror_helpers() -> tuple[Any, Any] | None:
    global _MIRROR_HELPERS, _MIRROR_HELPERS_FAILED
    if _MIRROR_HELPERS is not None:
        return _MIRROR_HELPERS
    if _MIRROR_HELPERS_FAILED:
        return None
    try:
        from tesseract.mirror.server.envelope import make_envelope
        from tesseract.mirror.server.session import send_envelope
    except Exception:
        log.exception(
            "worker broadcast: mirror envelope/session import failed; "
            "subsequent broadcasts will silently no-op"
        )
        _MIRROR_HELPERS_FAILED = True
        return None
    _MIRROR_HELPERS = (make_envelope, send_envelope)
    return _MIRROR_HELPERS


async def broadcast_worker_event(
    app: Any,
    event_type: str,
    record: WorkerRecord,
) -> None:
    """Fan a ``worker_record_*`` envelope to every Mirror WS session.

    Mirror server boot wires this through ``set_worker_broadcast_hook``.
    Never raises — broadcast failure must not affect the originating
    mutation. ``app`` may be None → no-op.
    """
    if event_type not in VALID_EVENT_TYPES:
        log.warning("worker broadcast: unknown event_type %r", event_type)
        return
    if app is None or not hasattr(app, "get"):
        return
    sessions = app.get("server_sessions") or {}
    if not sessions:
        return
    helpers = _load_mirror_helpers()
    if helpers is None:
        return
    make_envelope, send_envelope = helpers
    payload = record.model_dump(mode="json")
    for sess in list(sessions.values()):
        env = make_envelope(
            event_type,
            "workers",
            getattr(sess, "session_id", ""),
            payload,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception(
                "worker broadcast: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )


__all__ = [
    "WorkerBroadcastHook",
    "VALID_EVENT_TYPES",
    "broadcast_worker_event",
    "fire_worker_broadcast",
    "set_worker_broadcast_hook",
]
