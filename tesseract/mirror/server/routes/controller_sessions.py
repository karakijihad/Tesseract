from __future__ import annotations

from aiohttp import web

OPERATOR_FACING_ORIGINS = frozenset({"mirror", "cli"})


def _list_active_sessions():
    from tesseract.orchestrator.tars_controller.sessions import (
        SessionRegistry,
    )

    return SessionRegistry().list_sessions(status="active")


async def controller_sessions_handler(request: web.Request) -> web.Response:
    out = []
    for rec in _list_active_sessions():
        origin = str(getattr(rec, "origin", "") or "")
        out.append(
            {
                "session_id": rec.session_id,
                "origin": origin,
                "status": str(getattr(rec, "status", "")),
                "title": getattr(rec, "title", None),
                "last_active_at": getattr(rec, "last_active_at", None),
                "operator_facing": origin in OPERATOR_FACING_ORIGINS,
            }
        )
    return web.json_response({"sessions": out})


async def controller_session_status_handler(request: web.Request) -> web.Response:
    """Single controller session by id — used by ControllerMirrorBlock to
    show a detached session's outcome after the live WS closes (manual
    reload or real disconnect). 404 when the id is unknown."""
    from tesseract.orchestrator.tars_controller.sessions import SessionRegistry

    session_id = request.match_info["session_id"]
    try:
        rec = SessionRegistry().get_session(session_id)
    except ValueError:
        return web.json_response(
            {"error": "invalid_session_id", "session_id": session_id}, status=400
        )
    if rec is None:
        return web.json_response(
            {"error": "not_found", "session_id": session_id}, status=404
        )
    # X-2 (2026-06-02) — surface ``transcript_path`` (under
    # ``<TESSERACT_HOME>/tars_controller/transcripts/``) so the Mirror
    # completion card can show the operator where to find the on-disk
    # transcript after the live WS has dropped.
    return web.json_response(
        {
            "session_id": rec.session_id,
            "status": rec.status,
            "mode": rec.mode,
            "origin": rec.origin,
            "title": rec.title,
            "last_active_at": rec.last_active_at,
            "transcript_path": rec.transcript_path,
        }
    )
