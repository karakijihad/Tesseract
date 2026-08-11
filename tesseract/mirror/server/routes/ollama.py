from __future__ import annotations

import logging

from aiohttp import web

from tesseract.mirror.server.routes._localhost import is_localhost_request

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
    return web.json_response(_status_body(await sup.status()))


def _status_body(s) -> dict:
    return {
        "running": s.running,
        "base_url": s.base_url,
        "embedding_model": s.embedding_model,
        "tags": s.tags,
        "embedding_present": s.embedding_present,
        # Without this the panel cannot tell a model that is present from one
        # the daemon could not be asked about: `embedding_present` is reported
        # True in both cases, deliberately, so an unreadable tag list does not
        # raise a false "missing" badge. `tags_error` is what carries the
        # difference.
        "tags_error": s.tags_error,
        "owned_by_mirror": s.owned_by_mirror,
        "binary_present": s.binary_present,
        "installing": s.installing,
        "install_error": s.install_error,
    }


_ACTIONS = ("start", "stop", "install")


async def action(request: web.Request) -> web.Response:
    """Start, stop, or install Ollama. Body: `{"action": ...}`.

    Stop is safe-by-default: only kills processes Mirror itself spawned;
    refuses (409) for externally-started daemons so we don't take down
    something another app depends on.

    Install is the operator-initiated recovery for a machine whose first-run
    install was blocked or declined — the per-launch retry runs with
    `--no-install` on purpose, so without this the only path back was a
    hand-typed command. It returns once the work is SCHEDULED; the download can
    run for the better part of an hour, and holding the request open for it
    would turn any client timeout into a false failure report. `installing` and
    `install_error` in the status body carry the outcome.

    Same-machine callers only. `install` downloads a vendor installer and
    runs it, so this handler is a code-execution trigger — the widest one
    the server exposes. The bind is 127.0.0.1, but CORS deliberately lets
    an `Origin`-less request through (that is what a native client looks
    like), so nothing above this line is actually checking who asked."""
    if not is_localhost_request(request):
        log.warning(
            "refused %s from %s — /api/system/ollama is same-machine only",
            request.method,
            request.remote,
        )
        return web.json_response({"error": "not_found"}, status=404)
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
    if op not in _ACTIONS:
        return web.json_response(
            {"error": f"action must be one of {', '.join(_ACTIONS)}"}, status=400
        )
    ok, msg = await getattr(sup, op)()
    if not ok:
        return web.json_response({"error": msg}, status=409)
    return web.json_response({"ok": True, "message": msg, **_status_body(await sup.status())})
