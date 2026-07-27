"""Operator-facing MCP approval routes (P3 session 3).

The operator resolves pending ASK-over-MCP approvals here — NOT over the MCP
bearer path (the approver is the local operator, not the remote client).

  GET  /api/mcp/approvals                    → list pending approvals
  POST /api/mcp/approvals/{approval_id}/decision  {approved: bool}

Resolving a pending approval unblocks the held ``tools/call`` request, which
then executes the verb (approved) or returns an error result (declined).
"""

from __future__ import annotations

from aiohttp import web

from tesseract.mirror.server.approvals_parse import (
    ApprovalDecisionError,
    parse_approved,
)


def _registry(app: web.Application):
    return app.get("mcp_approvals")


async def list_approvals(request: web.Request) -> web.Response:
    registry = _registry(request.app)
    if registry is None:
        return web.json_response({"items": []})
    return web.json_response({"items": registry.pending()})


async def decide_approval(request: web.Request) -> web.Response:
    registry = _registry(request.app)
    if registry is None:
        return web.json_response({"error": "mcp approvals not ready"}, status=503)
    approval_id = request.match_info["approval_id"]
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"error": "body must be JSON"}, status=400)
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
