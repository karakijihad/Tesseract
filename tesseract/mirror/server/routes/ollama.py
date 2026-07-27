from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)


async def status(request: web.Request) -> web.Response:
    """Read-only Ollama health probe. Drives the Settings LocalModels row
    (running indicator, embedding-model presence, owned-by-mirror flag)."""
    sup = request.app.get("ollama_supervisor")
    if sup is None:
        return web.json_response(
            {"error": "ollama_supervisor unavailable — see startup log"},
            status=503,
        )
    s = await sup.status()
    return web.json_response({
        "running": s.running,
        "base_url": s.base_url,
        "embedding_model": s.embedding_model,
        "tags": s.tags,
        "embedding_present": s.embedding_present,
        "owned_by_mirror": s.owned_by_mirror,
    })


async def action(request: web.Request) -> web.Response:
    """Start or stop Ollama. Body: `{"action": "start" | "stop"}`.

    Stop is safe-by-default: only kills processes Mirror itself spawned;
    refuses (409) for externally-started daemons so we don't take down
    something another app depends on."""
    sup = request.app.get("ollama_supervisor")
    if sup is None:
        return web.json_response(
            {"error": "ollama_supervisor unavailable"}, status=503
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    op = body.get("action")
    if op == "start":
        ok, msg = await sup.start()
        if not ok:
            return web.json_response({"error": msg}, status=409)
        s = await sup.status()
        return web.json_response({
            "ok": True,
            "message": msg,
            "running": s.running,
            "embedding_present": s.embedding_present,
            "owned_by_mirror": s.owned_by_mirror,
        })
    if op == "stop":
        ok, msg = await sup.stop()
        if not ok:
            return web.json_response({"error": msg}, status=409)
        s = await sup.status()
        return web.json_response({
            "ok": True,
            "message": msg,
            "running": s.running,
            "embedding_present": s.embedding_present,
            "owned_by_mirror": s.owned_by_mirror,
        })
    return web.json_response(
        {"error": "action must be 'start' or 'stop'"}, status=400
    )
