"""Cross-process WS notify for workspace event appends.

Mirror background subsystems (scheduler jobs, mission reflector hook) call
``broadcast_workspace_event(app, event)`` after writing to ``EventStore`` so
the operator's open Workspace inbox refreshes without a manual reload.
Writers without an ``app`` handle (e.g. kernel tools) skip the broadcast —
the next inbox fetch picks the event up from disk.

Pattern mirrors :func:`tesseract.scheduler.engine._broadcast_envelope`:
walks ``app["server_sessions"]`` and lazily resolves the Mirror envelope
helpers so this module stays import-safe in REPL / standalone contexts.
"""

from __future__ import annotations

import logging
from typing import Any

from tesseract.workspace_events.events import WorkspaceComment, WorkspaceEvent

log = logging.getLogger(__name__)

_MIRROR_HELPERS: tuple[Any, Any] | None = None
_MIRROR_HELPERS_FAILED = False


def _load_mirror_helpers() -> tuple[Any, Any] | None:
    """Resolve + cache (make_envelope, send_envelope). Returns None and stays
    None if the Mirror package isn't importable so we don't re-throw on every
    append."""
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
            "workspace broadcast: mirror envelope/session import failed; "
            "subsequent broadcasts will silently no-op"
        )
        _MIRROR_HELPERS_FAILED = True
        return None
    _MIRROR_HELPERS = (make_envelope, send_envelope)
    return _MIRROR_HELPERS


async def broadcast_workspace_event(app: Any, event: WorkspaceEvent) -> None:
    """Fan a ``workspace_event_appended`` envelope out to every Mirror WS.

    Never raises — workspace event broadcast must not fail the originating
    write. ``app`` may be None (REPL/standalone) → no-op.
    """
    if app is None or not hasattr(app, "get"):
        return
    sessions = app.get("server_sessions") or {}
    if not sessions:
        return
    helpers = _load_mirror_helpers()
    if helpers is None:
        return
    make_envelope, send_envelope = helpers
    payload = event.to_dict()
    for sess in list(sessions.values()):
        env = make_envelope(
            "workspace_event_appended",
            "workspace",
            getattr(sess, "session_id", ""),
            payload,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception(
                "workspace broadcast: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )


async def broadcast_thread_pending(
    app: Any,
    *,
    event_id: str,
    comment_id: str,
    state: str,
) -> None:
    """Fan a ``workspace_thread_pending`` envelope to attached Mirror WS.

    Emitted by the synthetic workspace-turn lifecycle in
    :mod:`tesseract.mirror.server.ws`:

    - ``state="queued"``  — another turn is running; this comment is queued.
    - ``state="thinking"`` — synthetic turn just spawned for this comment.
    - ``state="cleared"`` — turn finished (reply landed, rolled back, or
      cancelled). The frontend uses this to drop the indicator row.

    Fail-soft like the other broadcasters here.
    """
    if app is None or not hasattr(app, "get"):
        return
    sessions = app.get("server_sessions") or {}
    if not sessions:
        return
    helpers = _load_mirror_helpers()
    if helpers is None:
        return
    make_envelope, send_envelope = helpers
    payload = {"event_id": event_id, "comment_id": comment_id, "state": state}
    for sess in list(sessions.values()):
        env = make_envelope(
            "workspace_thread_pending",
            "workspace",
            getattr(sess, "session_id", ""),
            payload,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception(
                "workspace thread_pending broadcast: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )


async def broadcast_comment_appended(app: Any, comment: WorkspaceComment) -> None:
    """Fan a ``workspace_comment_appended`` envelope out to every Mirror WS.

    Operator comments fire from the REST `post_comment` handler; TARS
    replies fire from the post-tool-call hook in `ws.py` (which observes
    workspace_reply TOOL_RESULT). Same fail-soft semantics as
    `broadcast_workspace_event` — never raise, no-op when no app/session.
    """
    if app is None or not hasattr(app, "get"):
        return
    sessions = app.get("server_sessions") or {}
    if not sessions:
        return
    helpers = _load_mirror_helpers()
    if helpers is None:
        return
    make_envelope, send_envelope = helpers
    payload = comment.to_dict()
    for sess in list(sessions.values()):
        env = make_envelope(
            "workspace_comment_appended",
            "workspace",
            getattr(sess, "session_id", ""),
            payload,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception(
                "workspace comment broadcast: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )
