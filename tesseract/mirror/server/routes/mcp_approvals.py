"""Operator-facing MCP approval routes (P3 session 3).

The operator resolves pending ASK-over-MCP approvals here — NOT over the MCP
bearer path (the approver is the local operator, not the remote client).

  GET  /api/mcp/approvals?session_id=…             → list pending approvals
  POST /api/mcp/approvals/{approval_id}/decision   {approved, session_id}

Resolving a pending approval unblocks the held ``tools/call`` request, which
then executes the verb (approved) or returns an error result (declined).

Both routes require a live operator chat session, matching ``agenda.py`` and
``autonomy.py``. Localhost alone is not the credential here and could not be:
the client being asked about runs on this machine too, so an unauthenticated
route lets a held CLI approve its own write. What distinguishes the operator
is a connected chat session with a live ``ask_fn`` — the frontend holds one,
a lane does not. The list is gated as well as the decision: the pending set
names the verb and the client, which is the enumeration half of the same
question.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from tesseract.mirror.server.approvals_parse import (
    ApprovalDecisionError,
    parse_approved,
)


def _registry(app: web.Application):
    return app.get("mcp_approvals")


def _require_operator_session(
    request: web.Request, session_id: Any
) -> web.Response | None:
    """Mirror ``agenda.py::_require_operator_session`` — the session_id must
    resolve to an operator chat session holding a live ``ask_fn``."""
    if not isinstance(session_id, str) or not session_id:
        return web.json_response(
            {"error": "session_id required (operator chat session)"},
            status=401,
        )
    server_session = (request.app.get("server_sessions") or {}).get(session_id)
    if server_session is None or getattr(
        getattr(server_session, "chat_session", None), "ask_fn", None,
    ) is None:
        return web.json_response(
            {"error": f"operator session {session_id!r} not connected"},
            status=401,
        )
    return None


async def list_approvals(request: web.Request) -> web.Response:
    err = _require_operator_session(request, request.query.get("session_id"))
    if err is not None:
        return err
    registry = _registry(request.app)
    if registry is None:
        return web.json_response({"items": []})
    return web.json_response({"items": registry.pending()})


async def decide_approval(request: web.Request) -> web.Response:
    approval_id = request.match_info["approval_id"]
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    err = _require_operator_session(request, body.get("session_id"))
    if err is not None:
        return err
    registry = _registry(request.app)
    if registry is None:
        return web.json_response({"error": "mcp approvals not ready"}, status=503)
    try:
        approved = parse_approved(body)
    except ApprovalDecisionError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if not registry.resolve(approval_id, approved):
        return web.json_response(
            {"error": "unknown or already-settled approval", "approval_id": approval_id},
            status=404,
        )
    return web.json_response({"approval_id": approval_id, "approved": approved})


def register(app: web.Application) -> None:
    app.router.add_get("/api/mcp/approvals", list_approvals)
    app.router.add_post("/api/mcp/approvals/{approval_id}/decision", decide_approval)
