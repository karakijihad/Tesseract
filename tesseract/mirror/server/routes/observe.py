from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from tesseract.mirror.server.envelope import make_envelope

log = logging.getLogger(__name__)

_VALID_MODES = {"meta", "maintenance"}


async def observe(request: web.Request) -> web.Response:
    observer = request.app.get("observer")
    if observer is None:
        return web.json_response({"observation": None, "reason": "observer_unavailable"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if "mode" not in body:
        return web.json_response(
            {"error": f"missing required key 'mode'; expected one of {sorted(_VALID_MODES)}"},
            status=400,
        )
    mode = body["mode"]
    if mode not in _VALID_MODES:
        return web.json_response(
            {"error": f"invalid mode; expected one of {sorted(_VALID_MODES)}"},
            status=400,
        )

    session_id = body.get("session_id")
    history: list[dict[str, Any]]
    server_session = None
    if session_id:
        server_session = request.app.get("server_sessions", {}).get(session_id)
        if server_session is None:
            return web.json_response(
                {"error": f"unknown session_id {session_id!r}"}, status=404,
            )
        history = server_session.chat_session.history
    else:
        # Headless/curl path — caller supplies history. Unused by Mirror UI.
        history = body.get("history") or []

    try:
        result = await observer.observe(
            history=history,
            mode=mode,
            session_id=server_session.session_id if server_session else "",
        )
    except Exception as exc:
        log.exception("observer.observe failed")
        return web.json_response({"observation": None, "error": str(exc)}, status=500)

    if server_session is not None and not server_session.ws.closed:
        env = make_envelope(
            "observer_result",
            "background",
            server_session.session_id,
            {"mode": mode, "observation": result},
        )
        server_session.event_log.append(env)
        try:
            await server_session.ws.send_json(env)
        except ConnectionResetError:
            log.debug("ws closed mid-observer-emit for %s", server_session.session_id)

    return web.json_response({"observation": result or None})
