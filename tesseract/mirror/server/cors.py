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


def build_cors_middleware(allowed_origins: Iterable[str]) -> web.middleware:
    allowed = frozenset(allowed_origins) | PACKAGED_APP_ORIGINS

    @web.middleware
    async def cors_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        origin = request.headers.get("Origin", "")
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
