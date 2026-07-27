"""Y-1 — per-view canvas state persistence.

Endpoints:

- ``GET  /api/canvas/{view}`` — return the saved canvas blob for a view, or
  404 if the view has no saved state yet.
- ``POST /api/canvas/{view}`` — atomically write the canvas blob for a view.

POST (not PUT) for the write: the Mirror CORS middleware
(``mirror/server/cors.py``) only allows ``DELETE, GET, PATCH, POST,
OPTIONS`` cross-origin, and the dev frontend calls the backend
cross-origin (``lib/endpoints.ts``). Every mutation route in the codebase
uses POST for the same reason.

Canvas state lives at ``<TESSERACT_HOME>/workspace/canvas-state/<view>.json``
(operator-private, gitignored under ``tesseract/workspace/``). The blob is
the Surface Protocol persistence envelope. The file has two owners with
disjoint keys: the frontend owns ``tldraw_snapshot`` + ``viewport`` (it
POSTs them here on operator draw), and the backend ``SurfaceStore`` owns
``surfaces`` (Y-2). So on POST we preserve whatever ``surfaces`` are on
disk rather than let the frontend's payload clobber them. Both this route
and the SurfaceStore read-merge to keep the other owner's key; because each
read-modify-write is fully synchronous (no ``await`` between read and write)
under the single event loop and the write is atomic (``os.replace``), either
completion order leaves both keys intact (full invariant in
``orchestrator/surfaces/persistence``).

File I/O + path resolution live in ``orchestrator/surfaces/persistence`` so
the surface store and this route resolve ``<view>.json`` identically.
``canvas_state_dir`` is re-exported for the boot-time mkdir in ``app.py``.
"""

from __future__ import annotations

import logging

from aiohttp import web

from tesseract.orchestrator.surfaces.persistence import (
    canvas_state_dir,
    read_view_blob,
    safe_view,
    write_view_blob,
)

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

__all__ = ["canvas_state_dir", "get_canvas_state", "post_canvas_state", "register"]


async def get_canvas_state(request: web.Request) -> web.Response:
    view = safe_view(request.match_info["view"])
    if view is None:
        return web.json_response({"error": "invalid_view"}, status=400)
    data = read_view_blob(view)
    if data is None:
        # Disambiguate absent-file (404) from unreadable (read_view_blob
        # logs + returns None for both); a present-but-corrupt file is rare
        # and the operator's client treats either as "start empty".
        path = canvas_state_dir() / f"{view}.json"
        if path.exists():
            return web.json_response({"error": "read_failed"}, status=500)
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(data)


async def post_canvas_state(request: web.Request) -> web.Response:
    view = safe_view(request.match_info["view"])
    if view is None:
        return web.json_response({"error": "invalid_view"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_schema"}, status=400)
    if body.get("schema_version") != _SCHEMA_VERSION:
        return web.json_response(
            {"error": "invalid_schema", "detail": "schema_version must be 1"},
            status=400,
        )
    if body.get("view") != view:
        return web.json_response(
            {"error": "invalid_schema", "detail": "body.view must match path"},
            status=400,
        )

    # Preserve backend-owned surfaces — the frontend POSTs `surfaces: []`,
    # which must not wipe TARS-spawned cards.
    existing = read_view_blob(view)
    if existing is not None and "surfaces" in existing:
        body["surfaces"] = existing["surfaces"]

    try:
        write_view_blob(view, body)
    except OSError as exc:
        log.warning("canvas_state: write failed for %s: %s", view, exc)
        return web.json_response({"error": "write_failed"}, status=500)
    return web.json_response({"ok": True})


def register(app: web.Application) -> None:
    app.router.add_get("/api/canvas/{view}", get_canvas_state)
    app.router.add_post("/api/canvas/{view}", post_canvas_state)
