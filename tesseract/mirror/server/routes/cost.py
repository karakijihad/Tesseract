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


async def get_windows(request: web.Request) -> web.Response:
    """Spend over the last day, week and month, each beside the window before
    it.

    Separate from `get_state` because it is a different read: `state` is
    today's live totals, held in memory and pushed over the WS on every billed
    turn, while this replays the ledger file and is worth asking for when a
    panel opens rather than on every turn.
    """
    ledger = request.app.get("cost_ledger")
    if ledger is None:
        return web.json_response(
            {"error": "cost_ledger unavailable — see startup log"},
            status=503,
        )
    try:
        windows = ledger.windows()
    except Exception:
        log.exception("cost windows failed")
        return web.json_response({"error": "windows failed"}, status=500)
    return web.json_response(windows)
