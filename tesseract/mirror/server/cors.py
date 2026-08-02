from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Iterable

from aiohttp import web

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# The packaged app's own webview origins. Tauri serves the bundled UI from
# its private `tauri.localhost` origin (scheme differs per platform /
# `useHttpsScheme`), so its fetches to the local backend are cross-origin.
# These are intrinsic to the app — not operator-tunable infrastructure —
# and live in code so every existing install gains them via a git update
# (config seeding never rewrites an already-materialized mirror.yaml).
# Found live 2026-07-30: the first packaged install with a HEALTHY backend
# had every Settings fetch fail "Failed to fetch" while chat (WebSocket,
# not CORS-gated) worked.
PACKAGED_APP_ORIGINS = frozenset(
    {
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
    }
)


def resolve_allowed_origins(configured: Iterable[str]) -> frozenset[str]:
    """Every origin the local backend answers to: operator-configured plus the
    packaged webview's own. One allowlist so the CORS middleware and the
    WebSocket handshake gate cannot drift apart.
    """
    return frozenset(configured) | PACKAGED_APP_ORIGINS


def origin_is_allowed(origin: str, allowed: frozenset[str]) -> bool:
    """Whether a handshake carrying `origin` may proceed.

    A browser always sends `Origin` on a WebSocket handshake and cannot be made
    to forge it, so a present origin outside the allowlist is a cross-site
    caller. An absent one is a native client — the Tauri shell, a CLI, a test —
    which no web page can impersonate.
    """
    if not origin:
        return True
    return origin in allowed


# Methods that can change state. A cross-site page can issue any of these
# against loopback without a preflight — `Request.json` does not require a
# JSON content-type, so `text/plain` carrying JSON is enough to reach a
# handler. Reading is not gated: it is the writes that must prove origin.
_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def build_cors_middleware(allowed_origins: Iterable[str]) -> web.middleware:
    allowed = resolve_allowed_origins(allowed_origins)

    @web.middleware
    async def cors_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        origin = request.headers.get("Origin", "")
        if request.method in _STATE_CHANGING and not origin_is_allowed(origin, allowed):
            # Refuse before the handler runs. Decorating the response after the
            # fact — all this middleware used to do — leaves the write already
            # committed; CORS headers only tell a browser what it may read.
            raise web.HTTPForbidden(text="origin not allowed")
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            response = await handler(request)
        if origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = (
                "DELETE, GET, POST, OPTIONS"
                if request.path.startswith("/api/uploads/")
                else "DELETE, GET, PATCH, POST, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Vary"] = "Origin"
        return response

    return cors_middleware
