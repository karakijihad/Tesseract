"""MCP server — Streamable-HTTP transport embedded in the Mirror backend
(mcp-control-plane P4).

One spec-compliant endpoint, ``/mcp``:
  * ``POST`` — a JSON-RPC 2.0 message (``initialize`` / ``tools/list`` /
    ``tools/call`` / ``ping`` / notifications). The server replies with a single
    ``application/json`` JSON-RPC response (SSE responses are a server MAY we
    don't need — request/response suffices for the verb surface).
  * ``GET``  — the server→client SSE stream: the ``activity.watch``
    subscription. Every activity transition the caller owns is pushed as a
    JSON-RPC notification, resumable by ``Last-Event-ID``. See ``stream.py``
    for the contract.
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
from tesseract.mirror.server.mcp.approvals import MCPApprovalTimeout
from tesseract.mirror.server.mcp.audit import append_mcp_audit_row
from tesseract.mirror.server.mcp.auth import authenticate
from tesseract.mirror.server.mcp.dispatcher import MCPAskFn, MCPVerbDispatcher
from tesseract.mirror.server.mcp.session import MCPSessionRegistry
from tesseract.mirror.server.mcp.stream import (
    SSE_MIME,
    ActivityStreamHub,
    serve_activity_stream,
)
from tesseract.mirror.server.mcp.verbs import STREAM_VERB
from tesseract.orchestrator.activity.events import (
    subscribe_activity,
    unsubscribe_activity,
)

log = logging.getLogger(__name__)

_SESSION_HEADER = "Mcp-Session-Id"
_LAST_EVENT_HEADER = "Last-Event-ID"
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
        self._stream = ActivityStreamHub(
            replay_buffer=config.stream.replay_buffer,
            client_queue=config.stream.client_queue,
            max_streams_total=config.stream.max_streams_total,
            max_streams_per_session=config.stream.max_streams_per_session,
        )
        self._sessions = MCPSessionRegistry(
            clock=clock, on_close=self._stream.close_session
        )
        self._sweep_task: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self, app: web.Application) -> None:
        log.info(
            "mcp server ready: %d verb(s), %d client(s), max %d session(s)",
            len(self._config.verbs),
            len(self._config.clients),
            self._config.server.max_connections,
        )
        subscribe_activity(self._stream.publish)
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self, app: web.Application) -> None:
        """Cancel the idle sweep, detach from the activity feed, then close
        every live stream and session + its Activity record on shutdown."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweep_task
            self._sweep_task = None
        unsubscribe_activity(self._stream.publish)
        self._stream.close_all()
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
    # A session id is a bearer capability. GET has always checked that the
    # presenting client owns it; POST did not, so a second configured client
    # holding its own valid token could call verbs against — and be billed and
    # audited under — someone else's session.
    if session is not None and session.client.name != client.name:
        return _rpc_error(403, protocol._SERVER_ERROR, "session belongs to another client")
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


async def _get_handler(request: web.Request) -> web.StreamResponse:
    """Server→client SSE stream — the ``activity.watch`` subscription.

    Session-bound like every other non-``initialize`` request, and bound to the
    session's OWN client: the session id is a bearer capability, so a second
    configured client presenting its own valid token must still not ride it."""
    server = _server(request)
    if server is None:
        return _rpc_error(503, protocol._SERVER_ERROR, "mcp server not ready")
    client = authenticate(server._config, request.headers.get("Authorization"))
    if client is None:
        return _rpc_error(401, protocol._SERVER_ERROR, "unauthorized: valid bearer token required")
    if SSE_MIME not in (request.headers.get("Accept") or ""):
        return _rpc_error(
            406, protocol._SERVER_ERROR, f"GET /mcp requires 'Accept: {SSE_MIME}'"
        )
    session = server._sessions.get(request.headers.get(_SESSION_HEADER))
    if session is None:
        return _rpc_error(
            400, protocol._INVALID_REQUEST,
            "invalid or missing Mcp-Session-Id; call initialize first",
        )
    if session.client.name != client.name:
        return _rpc_error(403, protocol._SERVER_ERROR, "session belongs to another client")

    posture = server._dispatcher.resolve_posture(STREAM_VERB, client)
    if posture == "deny":
        await _audit_stream(client, posture, "deny")
        return _rpc_error(403, protocol._SERVER_ERROR, f"verb denied by policy: {STREAM_VERB}")
    # Claimed before the ASK prompt and released only after the pump returns,
    # so the slot bounds the whole connection — and so a flood of opens is
    # refused at the cap rather than turned into a flood of operator prompts.
    if not server._stream.reserve(session.session_id):
        await _audit_stream(client, posture, "at_capacity")
        return _rpc_error(
            429,
            protocol._SERVER_ERROR,
            "activity stream capacity reached; close an existing stream or retry",
        )
    try:
        if posture == "ask" and not await _stream_approved(server, client):
            await _audit_stream(client, posture, "declined")
            return _rpc_error(
                403, protocol._SERVER_ERROR, f"operator declined verb: {STREAM_VERB}"
            )

        server._sessions.touch(session.session_id)
        await _audit_stream(client, posture, "ok", summary="stream opened")
        return await serve_activity_stream(
            request,
            hub=server._stream,
            session_id=session.session_id,
            caller=client.name,
            heartbeat_s=server._config.stream.heartbeat_s,
            last_event_id=request.headers.get(_LAST_EVENT_HEADER),
            session_is_live=lambda: server._sessions.get(session.session_id) is not None,
            on_activity=lambda: server._sessions.touch(session.session_id),
        )
    finally:
        server._stream.release(session.session_id)


async def _stream_approved(server: MCPServer, client) -> bool:
    """An ASK posture on the stream verb. Unlike a ``tools/call``, there is no
    202 handle to degrade to — a stream is open or it is not — so an undecided
    subscription is a refused one."""
    if server._verb_ask_fn is None:
        return False
    try:
        return await server._verb_ask_fn(STREAM_VERB, {}, client)
    except MCPApprovalTimeout:
        return False


async def _audit_stream(client, posture: str, decision: str, summary: str = "") -> None:
    await append_mcp_audit_row(
        verb=STREAM_VERB,
        client=client.name,
        trust_tier=client.trust_tier,
        posture=posture,
        decision=decision,
        result_summary=summary,
    )


async def _delete_handler(request: web.Request) -> web.Response:
    """Terminate the session named by ``Mcp-Session-Id`` — the caller's own.

    Ownership is checked here for the same reason GET checks it: the session id
    is a bearer capability, and closing someone else's session kills their
    streams and flips their ``mcp_session`` Activity record to closed."""
    server = _server(request)
    if server is None:
        return _rpc_error(503, protocol._SERVER_ERROR, "mcp server not ready")
    client = authenticate(server._config, request.headers.get("Authorization"))
    if client is None:
        return _rpc_error(401, protocol._SERVER_ERROR, "unauthorized: valid bearer token required")
    session = server._sessions.get(request.headers.get(_SESSION_HEADER))
    if session is None:
        return _rpc_error(404, protocol._SERVER_ERROR, "unknown session")
    if session.client.name != client.name:
        return _rpc_error(403, protocol._SERVER_ERROR, "session belongs to another client")
    server._sessions.close(session.session_id)
    return web.Response(status=200)


__all__ = ["MCPServer"]
