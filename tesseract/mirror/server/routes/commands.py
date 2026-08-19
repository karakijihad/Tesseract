"""GET /api/commands — frontend autocomplete source.

Hydrates the Mirror chat slash palette from the unified
`commands_registry` rather than a hardcoded array. The list includes both
Mirror session ops (`source="mirror_session"`) and every kernel tool
(`source="kernel_tool"`) — see `commands_registry.py` for the merge rules
and security trust model.
"""

from __future__ import annotations

from aiohttp import web

from tesseract.mirror.server.commands_registry import serialize_specs


async def list_commands(request: web.Request) -> web.Response:
    registry = request.app.get("command_registry")
    if registry is None:
        # The boot graph's `wiring` layer hasn't built it yet — a 200
        # with an empty list used to be cached as "loaded with zero commands"
        # by the frontend, so every `/save` and `/reset` looked nonexistent
        # until the page reloaded. 503 lets the frontend distinguish "not
        # ready, retry" from "ready and genuinely empty".
        return web.json_response({"error": "registry_not_ready"}, status=503)
    return web.json_response({"commands": serialize_specs(registry.specs())})
