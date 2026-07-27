"""MCP server — Streamable-HTTP transport embedded in the Mirror backend
(mcp-control-plane P4).

One spec-compliant endpoint, ``/mcp``:
  * ``POST`` — a JSON-RPC 2.0 message (``initialize`` / ``tools/list`` /
    ``tools/call`` / ``ping`` / notifications). The server replies with a single
    ``application/json`` JSON-RPC response (SSE responses are a server MAY we
    don't need — request/response suffices for the verb surface).
  * ``GET``  — the optional server→client SSE stream. We push no
    server-initiated messages in this initiative → ``405`` (spec-compliant).
  * ``DELETE`` — terminate the session named by ``Mcp-Session-Id``.

Every request is bearer-authenticated against ``mcp.yaml::clients`` (default-
deny). ``initialize`` mints an ``Mcp-Session-Id`` = the ``mcp_session`` Activity
record id ("who's in the chair"). Verb calls flow through the same
``MCPVerbDispatcher`` → ``permissions.decide.evaluate`` → cost ledger → audit
stack as before — so no MCP path bypasses a gate.

This replaced the P2/P3 bespoke ``POST /mcp/call`` + ``GET /mcp/events`` verb
envelope (pruned once real MCP clients — Claude Code / Codex CLI — could speak
to it directly, per the operator's purge-legacy rule).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable

from aiohttp import web

from tesseract.config.mcp import MCPConfig
from tesseract.mirror.server.mcp import protocol
from tesseract.mirror.server.mcp.auth import authenticate
from tesseract.mirror.server.mcp.dispatcher import MCPAskFn, MCPVerbDispatcher
from tesseract.mirror.server.mcp.session import MCPSessionRegistry

log = logging.getLogger(__name__)

_SESSION_HEADER = "Mcp-Session-Id"
_PARSE_ERROR = -32700


def _rpc_error(status: int, code: int, message: str) -> web.Response:
    body = {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}
    return web.json_response(body, status=status)


class MCPServer:
    """Owns the MCP transport: config, the verb dispatcher, the live-session
    registry, and the MCP-verb operator-approval callback. One instance per
    Mirror app, held at ``app['mcp_server']``."""

    def __init__(
        self,
        config: MCPConfig,
        *,
        verb_ask_fn: MCPAskFn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._dispatcher = MCPVerbDispatcher(config)
        # MCP-verb operator-approval callback. None → ASK verbs return the async
        # awaiting_operator handle; wired in app.py to the Mirror approval route.
        self._verb_ask_fn = verb_ask_fn
        self._sessions = MCPSessionRegistry(clock=clock)
        self._sweep_task: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self, app: web.Application) -> None:
        log.info(
            "mcp server ready: %d verb(s), %d client(s), max %d session(s)",
            len(self._config.verbs),
            len(self._config.clients),
            self._config.server.max_connections,
        )
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self, app: web.Application) -> None:
        """Cancel the idle sweep, then close every live session + its Activity
        record on shutdown."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweep_task
            self._sweep_task = None
        self._sessions.close_all()

    async def _sweep_loop(self) -> None:
        """Idle-sweep zombie sessions (client vanished without ``DELETE
        /mcp``). Interval derives from ``idle_timeout_s`` — no second cadence
        key."""
        interval_s = self._config.server.idle_timeout_s / 4
        while True:
            await asyncio.sleep(interval_s)
            for session_id in self._sessions.sweep_idle(self._config.server.idle_timeout_s):
                log.info("mcp session idle-swept: %s", session_id)

    def cancel_session(self, activity_id: str) -> bool:
        """Terminate one MCP session by its ``mcp_session`` activity_id
        (activity.cancel). Returns False if no such session is live."""
        return self._sessions.close(activity_id)

    @staticmethod
    def register_routes(app: web.Application) -> None:
        app.router.add_post("/mcp", _post_handler)
        app.router.add_get("/mcp", _get_handler)
        app.router.add_delete("/mcp", _delete_handler)


def _server(request: web.Request) -> MCPServer | None:
    return request.app.get("mcp_server")


async def _post_handler(request: web.Request) -> web.Response:
    server = _server(request)
    if server is None:
        return _rpc_error(503, protocol._SERVER_ERROR, "mcp server not ready")
    client = authenticate(server._config, request.headers.get("Authorization"))
    if client is None:
        return _rpc_error(401, protocol._SERVER_ERROR, "unauthorized: valid bearer token required")

    try:
        message = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _rpc_error(400, _PARSE_ERROR, "request body must be JSON")

    session = server._sessions.get(request.headers.get(_SESSION_HEADER))
    if session is not None:
        server._sessions.touch(session.session_id)
    handled = await protocol.handle(
        server=server, app=request.app, client=client, session=session, message=message
    )
    if handled.response is None:
        return web.Response(status=202)  # notification — accepted, no body
    resp = web.json_response(handled.response)
    if handled.new_session is not None:
        resp.headers[_SESSION_HEADER] = handled.new_session.session_id
    return resp


async def _get_handler(request: web.Request) -> web.Response:
    """Server→client SSE stream. Unused in this initiative (no server-initiated
    messages) → 405 per the Streamable-HTTP spec."""
    server = _server(request)
    if server is None:
        return _rpc_error(503, protocol._SERVER_ERROR, "mcp server not ready")
    if authenticate(server._config, request.headers.get("Authorization")) is None:
        return _rpc_error(401, protocol._SERVER_ERROR, "unauthorized: valid bearer token required")
    return web.Response(status=405, headers={"Allow": "POST, DELETE"})


async def _delete_handler(request: web.Request) -> web.Response:
    """Terminate the session named by ``Mcp-Session-Id``."""
    server = _server(request)
    if server is None:
        return _rpc_error(503, protocol._SERVER_ERROR, "mcp server not ready")
    if authenticate(server._config, request.headers.get("Authorization")) is None:
        return _rpc_error(401, protocol._SERVER_ERROR, "unauthorized: valid bearer token required")
    session_id = request.headers.get(_SESSION_HEADER) or ""
    if server._sessions.close(session_id):
        return web.Response(status=200)
    return _rpc_error(404, protocol._SERVER_ERROR, "unknown session")


__all__ = ["MCPServer"]
