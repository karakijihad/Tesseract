"""GET /api/observer/stats — counter snapshot for the Mirror stats chip.

Returns 503 when no observer is configured so the frontend can render an
unavailable state rather than silently showing stale zeros.
"""

from __future__ import annotations

from aiohttp import web


async def stats(request: web.Request) -> web.Response:
    observer = request.app.get("observer")
    if observer is None:
        return web.json_response({"error": "observer unavailable"}, status=503)
    return web.json_response(observer.get_stats())
