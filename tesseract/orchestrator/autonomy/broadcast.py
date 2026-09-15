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

import asyncio
import logging
from typing import Any, Callable, Mapping

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

VALID_VERDICT_EVENT_TYPES = frozenset({"task_verdict_recorded"})

VALID_CLOSE_EVENT_TYPES = frozenset({"task_closed"})


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


def agenda_broadcast_hook(app: Any) -> Callable[[str, AgendaItem, Mapping[str, Any]], None]:
    """Build the sync hook `AgendaStore.set_broadcast_hook` takes, closed over
    one `app`.

    Any `AgendaStore` instance can be wired to this factory: the kernel's own
    store and the separate store `build_tool_registry` builds for
    task_propose/task_work/task_close/agenda_comment both fan through it, so
    a task closing on the second store reaches WS exactly like a kernel-
    internal mutation on the first. They are different instances and neither
    hook double-fires the other's events; route handlers keep their own
    manual `broadcast_agenda_event` calls independent of this.

    The store stays sync, so the hook resolves the running loop at fire time
    (mutations happen on the event loop thread) and schedules the async
    broadcast as a task. A loop that is stopping or closed at shutdown drops
    the broadcast silently, matching the cost-ledger broadcast hook pattern.
    """

    def _hook(event_type: str, item: AgendaItem, extras: Mapping[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        prior_status = extras.get("prior_status") if extras else None
        try:
            loop.create_task(
                broadcast_agenda_event(app, event_type, item, prior_status=prior_status),
                name=f"agenda_broadcast:{event_type}:{item.id}",
            )
        except RuntimeError:
            return

    return _hook


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


async def broadcast_close_event(
    app: Any, item_id: str, outcome: str, verified_by: str,
) -> None:
    """Fan a task's close out to every Mirror WS session, so the Day room
    refreshes without a reload.

    Fired exactly once, from `outcome_watch`'s poll loop, never from
    `task_close.py` or a route handler directly: a close written by the
    Mirror's own chat, by Telegram, or by an agent controller session with no
    `app` at all all land as the same row in `agenda/history/`, and the
    watcher is what turns any of them into this one broadcast. Never raises;
    `app` may be None.
    """
    event_type = "task_closed"
    if event_type not in VALID_CLOSE_EVENT_TYPES:  # pragma: no cover - fixed literal
        log.warning("close broadcast: unknown event_type %r", event_type)
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
    payload = {"item_id": item_id, "outcome": outcome, "verified_by": verified_by}
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
                "close broadcast: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )


async def broadcast_verdict_event(
    app: Any, task_id: str, verdict: str, by: str,
) -> None:
    """Fan the operator's one-key verdict out to every Mirror WS session.

    Fired only by `outcome_watch`'s `_announce_key`, once per key row it reads
    from `agenda/verdicts/`, never from a route or bridge handler directly: a
    cockpit button, a Telegram tap and a Telegram typed reply all write that
    file through `record_verdict`, so reading it is what makes every surface,
    in any process, show the key live without teaching any of them about WS.
    Never raises; `app` may be None.
    """
    event_type = "task_verdict_recorded"
    if event_type not in VALID_VERDICT_EVENT_TYPES:  # pragma: no cover - fixed literal
        log.warning("verdict broadcast: unknown event_type %r", event_type)
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
    payload = {"task": task_id, "verdict": verdict, "by": by}
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
                "verdict broadcast: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )
