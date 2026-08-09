"""Operator-facing parked-ask routes (trio W4 — ask-instead-of-die; extended
2026-07-13 with controller-origin asks, Option B).

Two independent parked-ask stores merge here:

* ``app["parked_asks"]`` — chat-origin (Mirror-process) asks, keyed by
  server-minted ``approval_id``; settling sets the future directly.
* ``app["controller_parked_asks"]`` — a VIEW of the controller daemon's own
  parked asks, kept live by ``ActivitySubscriber``; settling relays
  ``decide_parked_ask`` over that subscriber's IPC connection. This route
  never removes an entry on decide — authoritative removal comes from the
  daemon's ``controller_ask_settled`` broadcast, which the subscriber
  applies independently.

  GET  /api/asks/parked                       → list parked asks across sessions
  POST /api/asks/{approval_id}/decision       {approved: bool}

Unknown or already-settled approval_ids return 404 (bounded-hold semantics,
same as ASK-over-MCP).
"""

from __future__ import annotations

from aiohttp import web

from tesseract.mirror.server.approvals_parse import (
    ApprovalDecisionError,
    parse_approved,
)
from tesseract.orchestrator.agent_controller.ipc_client import ControllerClientError


async def list_parked(request: web.Request) -> web.Response:
    # App-level dicts (session.py::create_server_session seeds "parked_asks";
    # ActivitySubscriber seeds "controller_parked_asks") — parked entries must
    # outlive the WS session/subscriber connection that surfaced them.
    chat_items = [
        entry.to_wire() for entry in request.app.get("parked_asks", {}).values()
    ]
    controller_items = list(
        request.app.get("controller_parked_asks", {}).values()
    )
    return web.json_response({"items": chat_items + controller_items})


async def decide_parked(request: web.Request) -> web.Response:
    # M13 — resolve by the server-minted approval_id (collision-safe across
    # sessions), not the provider call_id which is only unique per session.
    approval_id = request.match_info["approval_id"]
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"error": "body must be JSON"}, status=400)
    try:
        approved = parse_approved(body)
    except ApprovalDecisionError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    entry = request.app.get("parked_asks", {}).get(approval_id)
    if entry is not None and not entry.future.done():
        entry.future.set_result(approved)
        return web.json_response(
            {"approval_id": approval_id, "call_id": entry.call_id, "approved": approved}
        )

    if approval_id in request.app.get("controller_parked_asks", {}):
        subscriber = request.app.get("activity_subscriber")
        client = getattr(subscriber, "client", None) if subscriber is not None else None
        if client is None:
            return web.json_response(
                {"error": "controller not connected — cannot settle parked ask"},
                status=503,
            )
        try:
            await client.decide_parked_ask(approval_id, approved)
        except ControllerClientError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        # Authoritative removal comes from the daemon's `controller_ask_
        # settled` broadcast (applied by ActivitySubscriber), not here.
        return web.json_response({"approval_id": approval_id, "approved": approved})

    return web.json_response(
        {"error": "unknown or already-settled parked ask", "approval_id": approval_id},
        status=404,
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/asks/parked", list_parked)
    app.router.add_post("/api/asks/{approval_id}/decision", decide_parked)
