"""MCP JSON-RPC 2.0 message router (mcp-control-plane P4).

The wire-protocol brain behind the Streamable-HTTP transport (``server.py``).
Given one parsed JSON-RPC message + the authenticated client + the resolved
session, it returns the JSON-RPC response (or ``None`` for a notification).

Supported methods: ``initialize`` (opens the session + its ``mcp_session``
Activity record), ``notifications/initialized`` (no-op ack), ``ping``,
``tools/list`` (the governed verb catalog), ``tools/call`` (→ the
``MCPVerbDispatcher``, reusing the full permission + cost + audit stack). Every
other method → JSON-RPC ``-32601`` method-not-found.

Session binding: ``initialize`` needs no session; every other request MUST
carry a valid ``Mcp-Session-Id`` (spec-compliant clients echo the one returned
by ``initialize``). A missing/unknown session → ``-32600`` so the client
re-initializes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import mcp.types as types

from tesseract.config.mcp import MCPClient
from tesseract.mirror.server.mcp.orientation import build_instructions
from tesseract.mirror.server.mcp.session import MCPSession
from tesseract.mirror.server.mcp.tools import call_result, list_tools, verb_for_tool

# Echo the client's requested version when supported, else fall back to latest.
_SUPPORTED_PROTOCOL = ("2025-06-18", "2025-03-26", "2024-11-05")
_LATEST_PROTOCOL = _SUPPORTED_PROTOCOL[0]
_SERVER_NAME = "tesseract"
_SERVER_VERSION = "1.0"

# Phase 2b — the standard JSON-RPC codes come from the SDK (numerically
# identical to the prior literals, so the wire is byte-for-byte unchanged; the
# constants just name them). `_SERVER_ERROR` stays a local literal: it's in the
# JSON-RPC implementation-defined range (-32000..-32099) and has no SDK
# constant (the SDK's INTERNAL_ERROR is -32603, a different code — swapping it
# would change the wire).
_INVALID_REQUEST = types.INVALID_REQUEST
_METHOD_NOT_FOUND = types.METHOD_NOT_FOUND
_INVALID_PARAMS = types.INVALID_PARAMS
_SERVER_ERROR = -32000


@dataclass
class Handled:
    """Router outcome. ``response`` is the JSON-RPC reply (``None`` for a
    notification → HTTP 202). ``new_session`` is set only by ``initialize`` so
    the transport can emit the ``Mcp-Session-Id`` header."""

    response: dict[str, Any] | None
    new_session: MCPSession | None = None


def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _negotiate(requested: Any) -> str:
    return requested if requested in _SUPPORTED_PROTOCOL else _LATEST_PROTOCOL


async def handle(
    *,
    server: Any,
    app: Any,
    client: MCPClient,
    session: MCPSession | None,
    message: Any,
) -> Handled:
    """Route one JSON-RPC message. ``session`` is the transport's lookup of the
    request's ``Mcp-Session-Id`` header (``None`` if absent/unknown)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return Handled(_err(None, _INVALID_REQUEST, "not a JSON-RPC 2.0 message"))

    method = message.get("method")
    msg_id = message.get("id")
    is_notification = "id" not in message

    # A response echoed back to us (has id, no method) — we issue no requests.
    if method is None:
        return Handled(None)
    if is_notification:
        # notifications/initialized and any other client notification → ack only.
        return Handled(None)

    if method == "initialize":
        params = _object_params(message)
        if params is None:
            return Handled(_err(msg_id, _INVALID_PARAMS, "initialize 'params' must be an object"))
        return await _initialize(server, client, msg_id, params)
    if method == "ping":
        return Handled(_ok(msg_id, {}))

    # Everything below is session-bound.
    if session is None:
        return Handled(
            _err(msg_id, _INVALID_REQUEST, "invalid or missing Mcp-Session-Id; call initialize first")
        )

    if method == "tools/list":
        result = types.ListToolsResult(
            tools=list_tools(server._dispatcher, client)
        ).model_dump(by_alias=True, exclude_none=True)
        return Handled(_ok(msg_id, result))
    if method == "tools/call":
        params = _object_params(message)
        if params is None:
            return Handled(_err(msg_id, _INVALID_PARAMS, "tools/call 'params' must be an object"))
        return await _tools_call(server, app, client, session, msg_id, params)

    return Handled(_err(msg_id, _METHOD_NOT_FOUND, f"method not found: {method}"))


def _object_params(message: dict[str, Any]) -> dict[str, Any] | None:
    """The request's ``params`` as an object, or None if it is not one.

    JSON-RPC permits ``params`` to be an array; every MCP method that takes
    params defines an object. An array otherwise reaches ``.get`` and raises,
    which the client sees as a transport failure rather than the parameter
    error it can actually act on.
    """
    params = message.get("params")
    if params is None:
        return {}  # absent or JSON null — the method's own defaults apply
    # `or {}` here would coerce every FALSEY non-object ([], 0, "", false) to
    # an empty dict and wave it through, while rejecting [1, 2] — the same
    # malformed input answered two ways depending on whether it was empty.
    return params if isinstance(params, dict) else None


async def _initialize(
    server: Any, client: MCPClient, msg_id: Any, params: dict[str, Any]
) -> Handled:
    if len(server._sessions) >= server._config.server.max_connections:
        return Handled(_err(msg_id, _SERVER_ERROR, "max MCP connections reached"))
    version = _negotiate(params.get("protocolVersion"))
    session = server._sessions.open(client, version)
    # `build_instructions` walks the memory store and vault to count them. That
    # is filesystem work proportional to the operator's history, and the loop
    # has WS heartbeats and live turns to service.
    instructions = await asyncio.to_thread(build_instructions)
    result = types.InitializeResult(
        protocolVersion=version,
        capabilities=types.ServerCapabilities(tools=types.ToolsCapability(listChanged=False)),
        serverInfo=types.Implementation(name=_SERVER_NAME, version=_SERVER_VERSION),
        instructions=instructions,
    ).model_dump(by_alias=True, exclude_none=True)
    return Handled(_ok(msg_id, result), new_session=session)


async def _tools_call(
    server: Any, app: Any, client: MCPClient, session: MCPSession, msg_id: Any, params: dict[str, Any]
) -> Handled:
    name = params.get("name")
    if not isinstance(name, str):
        return Handled(_err(msg_id, _INVALID_PARAMS, "tools/call requires a string 'name'"))
    verb = verb_for_tool(name)
    if verb is None:
        return Handled(_err(msg_id, _INVALID_PARAMS, f"unknown tool: {name}"))
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return Handled(_err(msg_id, _INVALID_PARAMS, "'arguments' must be an object"))
    status, body = await server._dispatcher.dispatch(
        app, verb, arguments, client, ask_fn=server._verb_ask_fn, session_id=session.session_id
    )
    return Handled(_ok(msg_id, call_result(status, body)))


__all__ = ["handle", "Handled"]
