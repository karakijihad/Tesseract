from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)


async def get_state(request: web.Request) -> web.Response:
    """Read-only snapshot of today's spend. Mirrors the WS catch-up envelope
    (`cost_state`) so non-WS surfaces (Settings panel "today's spend so far"
    rows, future routine job sync) can read the same shape without
    establishing a session."""
    ledger = request.app.get("cost_ledger")
    if ledger is None:
        return web.json_response(
            {"error": "cost_ledger unavailable — see startup log"},
            status=503,
        )
    try:
        snapshot = ledger.snapshot()
    except Exception:
        log.exception("cost snapshot failed")
        return web.json_response({"error": "snapshot failed"}, status=500)
    return web.json_response(snapshot)
