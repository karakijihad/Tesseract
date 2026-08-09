"""Operator activity controls — bulk and per-item actions over the activity
registry.

``POST /api/activity/close-all`` cancels every *cancellable* running unit in one
action; ``POST /api/activity/{activity_id}/close`` cancels a single one via the
same per-kind dispatch: lanes (controller IPC ``lane_close``), MCP sessions
(``MCPServer.cancel_session``), and background delegates (spawn-registry cancel).
Kinds with no single-unit close — ``controller_session`` / ``routine`` /
``autonomy`` — are skipped (each has its own lifecycle/verb).

A record in ``failed`` state (routine or autonomy — see
``activity/hooks.py::fail_routine`` / ``fail_autonomy``) is the exception:
"close" on it always means dismiss (remove-from-registry), regardless of
kind, checked before the cancellable-kind dispatch above. Failed chips
otherwise stay in the registry until the operator dismisses them — they are
not swept on a timer.

Operator-local (no MCP bearer; loopback-bound like the other ``/api`` routes).
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from tesseract.mirror.server.controller_ws import APP_FACTORY_KEY
from tesseract.orchestrator.agent_controller.lanes.principals import (
    OPERATOR_PRINCIPAL,
)
from tesseract.orchestrator.activity import get_activity_registry
from tesseract.orchestrator.agent_controller.ipc_client import (
    ControllerClient,
    ControllerClientError,
)

log = logging.getLogger(__name__)

_CANCELLABLE = {"lane", "mcp_session", "delegate"}
_REASON = "operator_close"
# Lane surfaces are opened on the cockpit `agent` view (canvas openActivity VIEW).
_LANE_SURFACE_VIEW = "orb"


def _dismiss_lane_surfaces(closed_lane_activity_ids: list[str]) -> None:
    """Close the surface card for each closed lane so it stops polling a dead
    lane (the 502 storm). Mirrors the single-delete path (closeLane + close the
    card); best-effort — a surface hiccup must not fail close-all."""
    bare = {aid.split(":", 1)[1] for aid in closed_lane_activity_ids if ":" in aid}
    if not bare:
        return
    try:
        from tesseract.orchestrator.surfaces.store import get_surface_store

        store = get_surface_store()
        for d in store.list_for_view(_LANE_SURFACE_VIEW):
            props = d.get("props") if isinstance(d.get("props"), dict) else {}
            if d.get("type") == "lane" and props.get("lane_id") in bare:
                sid = d.get("surface_id") or d.get("id")
                if sid:
                    store.apply_event(view=_LANE_SURFACE_VIEW, surface_id=sid, event="closed", detail={})
    except Exception:
        log.exception("close-all: lane surface dismissal failed")


async def _cancel_delegate(app: web.Application, handle_id: str) -> bool:
    """Cancel a background delegate spawn across every session's SpawnRegistry
    (the app-shutdown iteration pattern)."""
    for sess in (app.get("server_sessions") or {}).values():
        spawns = getattr(getattr(sess, "chat_session", None), "spawns", None)
        if spawns is None:
            continue
        if await spawns.cancel(handle_id):
            return True
    return False


async def _close_lanes(
    app: web.Application, lane_ids: list[tuple[str, str]]
) -> tuple[list[str], list[str]]:
    """Close every lane over one controller IPC connection. ``lane_ids`` is a
    list of ``(activity_id, lane_id)`` pairs. Returns
    (closed_activity_ids, errored_activity_ids)."""
    closed: list[str] = []
    errored: list[str] = []
    factory = app.get(APP_FACTORY_KEY)
    try:
        client = await (factory() if factory else ControllerClient.connect())
    except ControllerClientError as exc:
        log.warning("close-all: controller offline, %d lane(s) not closed: %s", len(lane_ids), exc)
        return closed, [aid for aid, _ in lane_ids]
    try:
        for activity_id, lane_id in lane_ids:
            try:
                await client.lane_close(
                    lane_id, _REASON, caller_principal=OPERATOR_PRINCIPAL
                )
                closed.append(activity_id)
            except Exception:
                log.exception("close-all: lane_close failed for %s", lane_id)
                errored.append(activity_id)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()
    return closed, errored


async def close_all(request: web.Request) -> web.Response:
    registry = get_activity_registry()
    records = registry.snapshot()
    server = request.app.get("mcp_server")

    closed: list[str] = []
    errored: list[str] = []
    lane_targets: list[tuple[str, str]] = []

    for r in records:
        if r.state == "failed":
            registry.remove(r.activity_id)
            closed.append(r.activity_id)
            continue
        if r.kind not in _CANCELLABLE:
            continue
        if r.kind == "mcp_session":
            if server is not None:
                server.cancel_session(r.activity_id)
            registry.remove(r.activity_id)
            closed.append(r.activity_id)
        elif r.kind == "delegate":
            handle_id = r.activity_id.split(":", 1)[1] if ":" in r.activity_id else r.activity_id
            if await _cancel_delegate(request.app, handle_id):
                closed.append(r.activity_id)
            else:
                registry.remove(r.activity_id)  # no live spawn → drop the stale chip
                closed.append(r.activity_id)
        elif r.kind == "lane" and ":" in r.activity_id:
            lane_targets.append((r.activity_id, r.activity_id.split(":", 1)[1]))

    if lane_targets:
        lane_closed, lane_errored = await _close_lanes(request.app, lane_targets)
        closed.extend(lane_closed)
        errored.extend(lane_errored)
        _dismiss_lane_surfaces(lane_closed)  # drop the cards → stop the 502 polling

    skipped = [
        r.activity_id for r in records
        if r.kind not in _CANCELLABLE and r.state != "failed"
    ]
    log.info("close-all: closed=%d errored=%d skipped=%d", len(closed), len(errored), len(skipped))
    return web.json_response(
        {"closed": closed, "errored": errored, "skipped": skipped,
         "counts": {"closed": len(closed), "errored": len(errored), "skipped": len(skipped)}}
    )


async def close_one(request: web.Request) -> web.Response:
    """Cancel a single running unit (lane / mcp_session / delegate). Same
    per-kind dispatch as ``close_all``, scoped to one ``activity_id``."""
    activity_id = request.match_info["activity_id"]
    registry = get_activity_registry()
    record = registry.get(activity_id)
    if record is None:
        return web.json_response({"error": f"unknown activity: {activity_id}"}, status=404)
    if record.state == "failed":
        registry.remove(activity_id)
        return web.json_response({"closed": True})
    if record.kind not in _CANCELLABLE:
        return web.json_response(
            {"error": f"activity kind '{record.kind}' has no single-unit close"}, status=400
        )

    if record.kind == "mcp_session":
        server = request.app.get("mcp_server")
        if server is not None:
            server.cancel_session(activity_id)
        registry.remove(activity_id)
        return web.json_response({"closed": True})

    if record.kind == "delegate":
        handle_id = activity_id.split(":", 1)[1] if ":" in activity_id else activity_id
        if not await _cancel_delegate(request.app, handle_id):
            registry.remove(activity_id)  # no live spawn → drop the stale chip
        return web.json_response({"closed": True})

    # lane
    if ":" not in activity_id:
        return web.json_response({"error": f"malformed lane activity id: {activity_id}"}, status=400)
    lane_id = activity_id.split(":", 1)[1]
    lane_closed, _lane_errored = await _close_lanes(request.app, [(activity_id, lane_id)])
    if not lane_closed:
        return web.json_response({"error": "lane close failed"}, status=502)
    _dismiss_lane_surfaces(lane_closed)  # drop the card → stop the 502 polling
    return web.json_response({"closed": True})


def register(app: web.Application) -> None:
    app.router.add_post("/api/activity/close-all", close_all)
    app.router.add_post("/api/activity/{activity_id}/close", close_one)


__all__ = ["close_all", "close_one", "register"]
