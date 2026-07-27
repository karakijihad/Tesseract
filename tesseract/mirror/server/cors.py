from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Iterable

from aiohttp import web

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def build_cors_middleware(allowed_origins: Iterable[str]) -> web.middleware:
    allowed = frozenset(allowed_origins)

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
