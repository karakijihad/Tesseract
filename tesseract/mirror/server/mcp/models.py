"""MCP dispatcher result envelopes (Pydantic v2).

The ``MCPVerbDispatcher`` returns ``(http_status, body)`` where ``body`` is one
of these shapes; the P4 MCP transport (``tools.py::call_result``) translates
them into a JSON-RPC ``CallToolResult``. (The P2/P3 bespoke ``POST /mcp/call``
request envelope that once lived here was pruned when the real MCP protocol
layer landed.)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class MCPCallResponse(BaseModel):
    """Successful verb result. ``data`` shape is verb-specific."""

    verb: str
    ok: bool = True
    data: Any = None


class MCPErrorResponse(BaseModel):
    """Uniform error envelope. ``code`` mirrors the HTTP status."""

    verb: str | None = None
    ok: bool = False
    code: int
    error: str


class MCPPendingResponse(BaseModel):
    """ASK-over-MCP async handle (HTTP 202). The client observes the outcome
    via ``activity.watch``; the operator approves in Mirror. Returned when an
    ASK-posture verb has no wired ``ask_fn`` (Doclog 2026-07-01 §ASK-over-MCP)."""

    verb: str
    ok: bool = False
    status: str = "awaiting_operator"
    approval_id: str


__all__ = [
    "MCPCallResponse",
    "MCPErrorResponse",
    "MCPPendingResponse",
]
