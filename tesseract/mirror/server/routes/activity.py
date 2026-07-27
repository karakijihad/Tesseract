"""AS-1 Phase 5 — Unified Activity Registry REST surface.

``GET /api/activity`` returns the current activity snapshot — the REST
hydration the frontend reads on mount. The ``activity`` WS channel
(``ws.py::_activity_events_pump``) then streams live deltas; replay is
dropped there precisely because this endpoint is the catch-up path.

Backend is the source of truth; the Mirror only reflects. The snapshot is
a derived in-memory projection (delegates registered in-process; lanes +
controller sessions seeded from disk at boot and updated by the
controller→Mirror push subscriber).
"""

from __future__ import annotations

from aiohttp import web

from tesseract.orchestrator.activity import get_activity_registry


async def list_activity(request: web.Request) -> web.Response:
    snapshot = get_activity_registry().snapshot()
    return web.json_response({"items": [r.model_dump() for r in snapshot]})


def register(app: web.Application) -> None:
    app.router.add_get("/api/activity", list_activity)
