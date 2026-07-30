"""Frontend error intake — ``POST /api/client-log``.

The packaged app's webview console is invisible: a frontend crash
(window.onerror, unhandled promise rejection) previously left no trace
anywhere on disk (2026-07-30, "getting error — there is no log in
TESSERACT for the error"). The UI posts those events here and they land
in the backend's rotating log under the ``tesseract.client`` logger, so
one file answers "what went wrong on the screen".

Localhost-only single-operator server (same trust model as every other
route); payloads are size-capped, never interpreted.
"""

from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger("tesseract.client")

_MAX_MESSAGE = 4000
_MAX_SOURCE = 200


async def post_client_log(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is a 400, not a 500
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    message = str(body.get("message", ""))[:_MAX_MESSAGE].strip()
    if not message:
        return web.json_response({"ok": False, "error": "message required"}, status=400)
    source = str(body.get("source", ""))[:_MAX_SOURCE]
    level = str(body.get("level", "error")).lower()
    emit = log.warning if level == "warning" else log.error
    emit("client: %s%s", message, f" [{source}]" if source else "")
    return web.json_response({"ok": True})


def register(app: web.Application) -> None:
    app.router.add_post("/api/client-log", post_client_log)
