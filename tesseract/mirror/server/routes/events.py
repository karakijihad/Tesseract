from __future__ import annotations

from aiohttp import web


async def events(request: web.Request) -> web.Response:
    session_id = request.query.get("session_id")
    if not session_id:
        return web.json_response({"error": "missing session_id"}, status=400)

    since = request.query.get("since")
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        return web.json_response({"error": "limit must be int"}, status=400)

    logs = request.app.get("event_logs") or {}
    event_log = logs.get(session_id)
    if event_log is None:
        return web.json_response({"events": []})

    return web.json_response({"events": event_log.since(since, limit)})
