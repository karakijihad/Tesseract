"""CV-1 — Mirror REST bridge to the controller-owned lane substrate.

The ``LaneManager`` (and its ``NamedLaneManager`` binding layer) lives in the
controller daemon, a supervised sibling process. Mirror does not host a lane
manager; it reaches the daemon over IPC via ``ControllerClient`` — a fresh
connect per request, mirroring ``controller_ws.py``. This lets the canvas
``LaneRenderer`` poll a lane's events and drive it with follow-ups.

Endpoints (all return 503 ``controller_offline`` when the daemon is down):

- ``GET  /api/lanes/trio``                 — the trio definition (config).
- ``GET  /api/lanes/named``                — list named-lane bindings.
- ``POST /api/lanes/named/ensure``         — ensure a named lane (spawn).
- ``GET  /api/lanes/{lane_id}/status``     — fast status probe.
- ``GET  /api/lanes/{lane_id}/read?cursor=`` — events since cursor.
- ``POST /api/lanes/{lane_id}/send``       — follow-up message into the lane.
- ``POST /api/lanes/{lane_id}/attach``     — brain-restart recovery snapshot.

A ``controller_client_factory`` may be injected on the app (key shared with
``controller_ws.py``) so tests supply a stub without a real daemon.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

from tesseract.config.cockpit import load_trio_lanes
from tesseract.mirror.server.controller_ws import APP_FACTORY_KEY
from tesseract.orchestrator.tars_controller.ipc_client import (
    ControllerClient,
    ControllerClientError,
)
from tesseract.paths import TESSERACT_DIR

log = logging.getLogger(__name__)

ControllerClientFactory = Callable[[], Awaitable[Any]]


async def _connect(request: web.Request) -> Any:
    factory: ControllerClientFactory | None = request.app.get(APP_FACTORY_KEY)
    return await (factory() if factory else ControllerClient.connect())


async def _with_client(
    request: web.Request, fn: Callable[[Any], Awaitable[web.Response]]
) -> web.Response:
    """Open a fresh ControllerClient, run ``fn``, always close. Maps a
    daemon-unreachable error to 503 and a lane-level error to 502."""
    try:
        client = await _connect(request)
    except ControllerClientError as exc:
        return web.json_response(
            {"error": "controller_offline", "detail": str(exc)}, status=503
        )
    try:
        return await fn(client)
    except ControllerClientError as exc:
        return web.json_response({"error": "lane_error", "detail": str(exc)}, status=502)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            log.debug("lanes: client close failed", exc_info=True)


async def get_trio(request: web.Request) -> web.Response:
    try:
        return web.json_response({"lanes": load_trio_lanes()})
    except (OSError, KeyError, ValueError) as exc:
        return web.json_response(
            {"error": "trio_config_invalid", "detail": str(exc)}, status=500
        )


async def list_named(request: web.Request) -> web.Response:
    async def _run(client: Any) -> web.Response:
        records = await client.lane_named_list()
        return web.json_response({"named": records})

    return await _with_client(request, _run)


async def ensure_named(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    name = body.get("name")
    kind = body.get("kind")
    model = body.get("model")
    if not isinstance(name, str) or kind not in ("claude", "codex") or not isinstance(model, str):
        return web.json_response({"error": "invalid_request"}, status=400)
    working_dir = body.get("working_dir") or str(TESSERACT_DIR)

    async def _run(client: Any) -> web.Response:
        record = await client.lane_named_ensure(
            name=name, kind=kind, model=model, working_dir=working_dir
        )
        return web.json_response({"record": record})

    return await _with_client(request, _run)


async def lane_status(request: web.Request) -> web.Response:
    lane_id = request.match_info["lane_id"]

    async def _run(client: Any) -> web.Response:
        return web.json_response(await client.lane_status(lane_id))

    return await _with_client(request, _run)


async def lane_read(request: web.Request) -> web.Response:
    lane_id = request.match_info["lane_id"]
    cursor = request.query.get("cursor") or None

    async def _run(client: Any) -> web.Response:
        return web.json_response(await client.lane_read(lane_id, cursor))

    return await _with_client(request, _run)


async def lane_send(request: web.Request) -> web.Response:
    lane_id = request.match_info["lane_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return web.json_response({"error": "empty_message"}, status=400)

    async def _run(client: Any) -> web.Response:
        return web.json_response(await client.lane_send(lane_id, message))

    return await _with_client(request, _run)


async def lane_attach(request: web.Request) -> web.Response:
    lane_id = request.match_info["lane_id"]

    async def _run(client: Any) -> web.Response:
        return web.json_response(await client.lane_attach(lane_id))

    return await _with_client(request, _run)


async def lane_close(request: web.Request) -> web.Response:
    """Terminate a lane (operator 'delete' on the card). Distinct from
    dismissing the surface card — this kills the underlying CLI."""
    lane_id = request.match_info["lane_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "operator_close") if isinstance(body, dict) else "operator_close"

    async def _run(client: Any) -> web.Response:
        return web.json_response(await client.lane_close(lane_id, reason))

    return await _with_client(request, _run)


def register(app: web.Application) -> None:
    app.router.add_get("/api/lanes/trio", get_trio)
    app.router.add_get("/api/lanes/named", list_named)
    app.router.add_post("/api/lanes/named/ensure", ensure_named)
    app.router.add_get("/api/lanes/{lane_id}/status", lane_status)
    app.router.add_get("/api/lanes/{lane_id}/read", lane_read)
    app.router.add_post("/api/lanes/{lane_id}/send", lane_send)
    app.router.add_post("/api/lanes/{lane_id}/attach", lane_attach)
    app.router.add_post("/api/lanes/{lane_id}/close", lane_close)
