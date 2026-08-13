from __future__ import annotations

from pathlib import Path

from aiohttp import web

from tesseract.brain.session_store import (
    duplicate_session,
    list_archive,
    list_sessions,
    list_sessions_by_day,
    load_session,
    preview_session,
    rename_session,
    session_file,
)
from tesseract.paths import home_dir


def _sessions_dir() -> Path:
    """Call-time resolve so an app update replacing the code tree never
    strands this route on a stale path — mirrors `boot.py::SESSIONS_DIR`
    (`TESSERACT_HOME/sessions`), just resolved fresh on every call instead
    of frozen at import time."""
    return home_dir() / "sessions"

# Reason → HTTP status. Centralized so all three mutating routes return
# consistent codes regardless of which session_store function emitted the
# rejection.
_REASON_STATUS = {
    "not_found": 404,
    "invalid_name": 400,
    "exists": 409,
    "io_error": 500,
}


async def list_sessions_handler(request: web.Request) -> web.Response:
    entries = list_sessions(_sessions_dir(), limit=100)
    payload = [
        {
            "session_id": path.stem,
            "started_at": state.started_at,
            "ended_at": state.ended_at,
            "turn_count": state.turn_count,
            "model": state.model,
        }
        for path, state in entries
    ]
    return web.json_response({"sessions": payload})


async def list_sessions_by_day_handler(request: web.Request) -> web.Response:
    """Phase 1 — per-day grouped view. Active runs only (archive is a
    separate route). Returned newest-day-first; runs within a day
    sorted newest-started first."""
    days = list_sessions_by_day(_sessions_dir())
    return web.json_response({"days": days})


async def list_archive_handler(request: web.Request) -> web.Response:
    """Phase 1 — archived per-run sessions, lazy-fetched only when the
    operator expands the archive section. Sorted newest-started first."""
    rows = list_archive(_sessions_dir())
    return web.json_response({"sessions": rows})


async def get_session(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    path = session_file(_sessions_dir(), session_id)
    if path is None:
        return web.json_response({"error": "not_found"}, status=404)
    # Inspect/export path — no strip. Real resume goes through the WS
    # `cmd_load` / `cmd_compact_file` handlers, which already strip
    # stale reasoning before replacing chat history. Leaving this route
    # unstripped means a future debug/inspect caller sees disk-truth.
    state = load_session(path)
    if state is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({
        "session_id": session_id,
        "schema": state.schema,
        "started_at": state.started_at,
        "ended_at": state.ended_at,
        "turn_count": state.turn_count,
        "model": state.model,
        "history": state.history,
    })


async def get_preview(request: web.Request) -> web.Response:
    """First N user/assistant turns — text only, used by SessionDrawer hover."""
    session_id = request.match_info["session_id"].removesuffix(".json")
    preview = preview_session(_sessions_dir(), session_id, max_turns=6)
    if preview is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(preview)


async def post_rename(request: web.Request) -> web.Response:
    # `save_name` is stored without the `.json` suffix; normalise the URL
    # segment before comparing so callers can pass either form.
    session_id = request.match_info["session_id"].removesuffix(".json")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    new_name = (body.get("new_name") or "").strip()
    ok, reason = rename_session(_sessions_dir(), session_id, new_name)
    if not ok:
        return web.json_response(
            {"error": reason}, status=_REASON_STATUS.get(reason, 500),
        )
    new_id = new_name.removesuffix(".json")
    sessions = request.app.get("server_sessions") or {}
    for srv in sessions.values():
        if srv.save_name == session_id:
            srv.save_name = new_id
    return web.json_response({"ok": True, "session_id": new_id})


async def post_duplicate(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"].removesuffix(".json")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    dest = (body.get("dest_name") or "").strip()
    ok, reason = duplicate_session(_sessions_dir(), session_id, dest)
    if not ok:
        return web.json_response(
            {"error": reason}, status=_REASON_STATUS.get(reason, 500),
        )
    return web.json_response({"ok": True, "session_id": dest.removesuffix(".json")})
