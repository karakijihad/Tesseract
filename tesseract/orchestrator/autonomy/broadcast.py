"""Cross-process WS notify for agenda mutations.

Mirror REST handlers call ``broadcast_agenda_event(app, event_type, item)``
after a successful agenda store mutation so every open Autonomy tab
refreshes without polling. Writers without an ``app`` handle (REPL,
standalone scheduler invocations) skip the broadcast — the next REST
fetch picks the state up from disk.

Pattern mirrors :func:`tesseract.workspace_events.broadcast.broadcast_workspace_event`:
walks ``app["server_sessions"]`` and lazily resolves the Mirror envelope
helpers so this module stays import-safe in REPL / standalone contexts.
"""

from __future__ import annotations

import logging
from typing import Any

from tesseract.orchestrator.autonomy.models import AgendaItem

log = logging.getLogger(__name__)

_MIRROR_HELPERS: tuple[Any, Any] | None = None
_MIRROR_HELPERS_FAILED = False

VALID_EVENT_TYPES = frozenset(
    {"agenda_item_added", "agenda_item_updated", "agenda_item_transitioned"}
)

VALID_COMMENT_EVENT_TYPES = frozenset({"agenda_comment_added"})

VALID_GOVERNOR_EVENT_TYPES = frozenset(
    {"governor_pause_added", "governor_pause_removed", "governor_tick"}
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
            "agenda broadcast: mirror envelope/session import failed; "
            "subsequent broadcasts will silently no-op"
        )
        _MIRROR_HELPERS_FAILED = True
        return None
    _MIRROR_HELPERS = (make_envelope, send_envelope)
    return _MIRROR_HELPERS


async def broadcast_agenda_event(
    app: Any,
    event_type: str,
    item: AgendaItem,
    *,
    prior_status: str | None = None,
) -> None:
    """Fan an agenda mutation envelope out to every Mirror WS session.

    Never raises — broadcast failure must not affect the originating
    mutation. ``app`` may be None → no-op.
    """
    if event_type not in VALID_EVENT_TYPES:
        log.warning("agenda broadcast: unknown event_type %r", event_type)
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
    payload: dict[str, Any] = item.model_dump(mode="json")
    if prior_status is not None:
        payload = {**payload, "_prior_status": prior_status}
    for sess in list(sessions.values()):
        env = make_envelope(
            event_type,
            "agenda",
            getattr(sess, "session_id", ""),
            payload,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception(
                "agenda broadcast: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )


async def broadcast_agenda_comment_event(
    app: Any,
    event_type: str,
    *,
    item_id: str,
    comment: dict[str, Any],
) -> None:
    """Fan an agenda-comment envelope to every Mirror WS session.

    Payload shape: ``{item_id, comment: {id, at, role, by, body}}`` —
    smaller than the full AgendaItem so the operator's textarea poll
    stays cheap. Frontend listeners append to the cached thread when
    the ``item_id`` matches the currently open detail modal.
    """
    if event_type not in VALID_COMMENT_EVENT_TYPES:
        log.warning("agenda comment broadcast: unknown event_type %r", event_type)
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
    payload = {"item_id": item_id, "comment": comment}
    for sess in list(sessions.values()):
        env = make_envelope(
            event_type,
            "agenda",
            getattr(sess, "session_id", ""),
            payload,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception(
                "agenda comment broadcast: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )


async def broadcast_governor_event(
    app: Any,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Fan a governor envelope (pause add/remove or tick) to every Mirror
    WS session. Mirrors :func:`broadcast_agenda_event` in shape; never
    raises. ``app`` may be None → no-op.
    """
    if event_type not in VALID_GOVERNOR_EVENT_TYPES:
        log.warning("governor broadcast: unknown event_type %r", event_type)
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
    body = dict(payload or {})
    for sess in list(sessions.values()):
        env = make_envelope(
            event_type,
            "governor",
            getattr(sess, "session_id", ""),
            body,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception(
                "governor broadcast: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )
