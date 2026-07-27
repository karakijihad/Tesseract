"""Y-2 — Surface Protocol REST surface.

- ``GET  /api/surfaces/{view}`` — list the view's surface descriptors
  (hydrated from the canvas-state file on first touch). The frontend store
  calls this on canvas mount to render TARS-spawned surfaces that predate
  the live WS connection.
- ``POST /api/surfaces/{view}`` — create a surface (operator/frontend half
  of ``surface.create``; the same store path the ``surface_create`` kernel
  tool uses). Returns the new ``surface_id`` + descriptor.
- ``POST /api/surfaces/{view}/event`` — operator interaction event
  (``moved`` / ``resized`` / ``closed`` / …). The canvas → tool half of
  ``surface.emit_event``. Persists the new geometry so a reload re-renders
  the surface where the operator left it.

POST (not PUT) for the write — the Mirror CORS middleware only allows
``DELETE/GET/PATCH/POST/OPTIONS`` cross-origin and the dev frontend calls
the backend cross-origin. Surface state lives in the shared process-wide
``SurfaceStore`` (orchestrator/surfaces); both these routes and the
``surface_*`` kernel tools mutate the same singleton.
"""

from __future__ import annotations

import logging

from aiohttp import web

from tesseract.orchestrator.surfaces.persistence import safe_view
from tesseract.orchestrator.surfaces.store import get_surface_store

log = logging.getLogger(__name__)

_OPERATOR_EVENTS = {"moved", "resized", "closed", "clicked", "edited", "highlighted"}


async def list_surfaces(request: web.Request) -> web.Response:
    view = safe_view(request.match_info["view"])
    if view is None:
        return web.json_response({"error": "invalid_view"}, status=400)
    return web.json_response({"view": view, "surfaces": get_surface_store().list_for_view(view)})


async def create_surface(request: web.Request) -> web.Response:
    view = safe_view(request.match_info["view"])
    if view is None:
        return web.json_response({"error": "invalid_view"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("type"), str):
        return web.json_response({"error": "invalid_schema", "detail": "type required"}, status=400)

    try:
        sid = get_surface_store().create(
            type=body["type"],
            view=view,
            props=body.get("props") if isinstance(body.get("props"), dict) else None,
            title=body.get("title"),
            position=body.get("position") if isinstance(body.get("position"), dict) else None,
            size=body.get("size") if isinstance(body.get("size"), dict) else None,
            mode=body.get("mode", "embedded"),
        )
    except Exception as exc:  # noqa: BLE001 — bad type/mode/geometry → 400
        return web.json_response({"error": "create_failed", "detail": str(exc)}, status=400)
    return web.json_response({"surface_id": sid, "surface": get_surface_store().get(sid)})


async def post_surface_event(request: web.Request) -> web.Response:
    view = safe_view(request.match_info["view"])
    if view is None:
        return web.json_response({"error": "invalid_view"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_schema"}, status=400)

    surface_id = body.get("surface_id")
    event = body.get("event")
    detail = body.get("detail") or {}
    if not isinstance(surface_id, str) or event not in _OPERATOR_EVENTS:
        return web.json_response({"error": "invalid_event"}, status=400)
    if not isinstance(detail, dict):
        return web.json_response({"error": "invalid_detail"}, status=400)

    try:
        ok = get_surface_store().apply_event(
            view=view, surface_id=surface_id, event=event, detail=detail
        )
    except (KeyError, TypeError, ValueError) as exc:
        return web.json_response({"error": "bad_detail", "detail": str(exc)}, status=400)
    if not ok:
        return web.json_response({"error": "unknown_surface"}, status=404)
    return web.json_response({"ok": True})


async def update_surface(request: web.Request) -> web.Response:
    """Operator edit of a surface (e.g. rename title). Routes to the same
    SurfaceStore.update the surface_update kernel tool uses."""
    view = safe_view(request.match_info["view"])
    if view is None:
        return web.json_response({"error": "invalid_view"}, status=400)
    surface_id = request.match_info["surface_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_schema"}, status=400)
    title = body.get("title")
    props = body.get("props") if isinstance(body.get("props"), dict) else None
    updated = get_surface_store().update(surface_id, props=props, title=title)
    if updated is None:
        return web.json_response({"error": "unknown_surface"}, status=404)
    return web.json_response({"surface": updated})


def register(app: web.Application) -> None:
    app.router.add_get("/api/surfaces/{view}", list_surfaces)
    app.router.add_post("/api/surfaces/{view}", create_surface)
    app.router.add_post("/api/surfaces/{view}/event", post_surface_event)
    app.router.add_post("/api/surfaces/{view}/{surface_id}/update", update_surface)
