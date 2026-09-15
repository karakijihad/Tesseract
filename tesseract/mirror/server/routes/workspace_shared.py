"""Plumbing shared by every `routes/workspace_*.py` module.

Small and dependency-free on purpose: `workspace.py` (the decision
dispatcher), `workspace_skill_commits.py`, `workspace_proposal_commits.py`
and `workspace_docs.py` all import from here rather than from each other, so
none of them has to import the dispatcher just to reach its own store handle.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from tesseract.workspace_events import EventStore

log = logging.getLogger(__name__)


def _store(app: web.Application) -> EventStore:
    store = app.get("workspace_event_store")
    if store is None:
        raise web.HTTPInternalServerError(reason="workspace_event_store not initialised")
    return store


async def _broadcast_envelope(app: web.Application, type_: str, data: dict[str, Any]) -> None:
    """Fan a session envelope to every connected Mirror WS. Best-effort."""
    sessions = app.get("server_sessions") or {}
    if not sessions:
        return
    try:
        from tesseract.mirror.server.envelope import make_envelope
        from tesseract.mirror.server.session import send_envelope
    except Exception:
        log.exception("workspace: mirror envelope/session import failed")
        return
    for sess in list(sessions.values()):
        env = make_envelope(type_, "session", getattr(sess, "session_id", ""), data)
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception("workspace: send_envelope failed")
