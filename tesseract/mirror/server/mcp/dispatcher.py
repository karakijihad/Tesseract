"""MCP verb dispatcher — the governed routing layer behind ``tools/call``.

One dispatcher per Mirror app. It owns verb-mapping + posture resolution +
the ASK gate + per-call audit, and delegates the actual permission decision
for tool-backed verbs to ``permissions.decide.evaluate`` (no duplication). The
MCP protocol layer (``protocol.py``/``tools.py``) translates a ``tools/call``
into ``dispatch()`` and its ``(status, body)`` result back into a
``CallToolResult``. It is a PARALLEL surface to ``CommandRegistry``, not an
extension of it, because ``CommandRegistry`` bakes in the Mirror-operator =
full-trust assumption an MCP client does not carry (Doclog 2026-07-01
§MCPVerbDispatcher).

Effective posture = ``strictest(verb floor, mcp.yaml verb posture, trust-tier
cap)``. A verb absent from the mcp.yaml allowlist is default-deny. ASK verbs
are gated by an injectable ``ask_fn``; with none wired they return an async
``awaiting_operator`` handle (HTTP 202) rather than hard-denying.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Awaitable, Callable

from aiohttp import web

from tesseract.config.mcp import MCPClient, MCPConfig, strictest
from tesseract.mirror.server.mcp.approvals import MCPApprovalTimeout
from tesseract.mirror.server.mcp.audit import append_mcp_audit_row
from tesseract.mirror.server.mcp.models import (
    MCPCallResponse,
    MCPErrorResponse,
    MCPPendingResponse,
)
from tesseract.mirror.server.mcp.verbs import (
    CALL_VERBS,
    STREAM_VERB,
    MCPPermissionDenied,
    MCPVerbError,
    VerbContext,
)

# (verb, params, client) -> approved. The MCP-verb-level operator gate — distinct
# from the tool-level ask_fn (tool, input, context) that decide.evaluate uses.
MCPAskFn = Callable[[str, dict[str, Any], MCPClient], Awaitable[bool]]

_RESULT_SUMMARY_CAP = 200


async def _always_true(tool: Any, tool_input: Any, context: Any) -> bool:
    """Tool-level ask_fn used AFTER the dispatcher's MCP-verb ASK gate has been
    satisfied — one operator approval covers the tool's own policy ASK too
    (absolute security/path DENY still fires inside decide.evaluate)."""
    return True


class MCPVerbDispatcher:
    def __init__(self, config: MCPConfig) -> None:
        self._config = config

    def resolve_posture(self, verb: str, client: MCPClient) -> str:
        """Effective posture for a verb+client. Default-deny for an
        un-allowlisted verb; otherwise the stricter of the mcp.yaml posture and
        the client's trust-tier cap. There is no source-side floor — the yaml
        is the authority, and it is DENY to TARS in `permissions.yaml`."""
        mcp_posture = self._config.verbs.get(verb)
        if mcp_posture is None:
            return "deny"
        return strictest(
            mcp_posture,
            self._config.trust_tier_cap(client.trust_tier),
        )

    async def dispatch(
        self,
        app: web.Application,
        verb: str,
        params: dict[str, Any],
        client: MCPClient,
        *,
        ask_fn: MCPAskFn | None = None,
        session_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Route one verb call. Returns ``(http_status, json_body)``. ``session_id``
        is the caller's ``mcp_session`` id (``mcp:<client>:<hex>``) so downstream
        tool records correlate to the exact session; it falls back to the client
        name for dispatcher-direct callers that carry no live session."""
        params_hash = _hash_params(params)
        if verb == STREAM_VERB:
            return self._err(400, f"{verb} is a streaming verb, not a tools/call verb", verb)
        if verb not in CALL_VERBS:
            await self._audit(verb, client, "n/a", "unknown_verb", params_hash=params_hash)
            return self._err(404, f"unknown verb: {verb}", verb)

        posture = self.resolve_posture(verb, client)
        if posture == "deny":
            await self._audit(verb, client, posture, "deny", params_hash=params_hash)
            return self._err(403, f"verb denied by policy: {verb}", verb)

        tool_ask_fn = None
        if posture == "ask":
            if ask_fn is None:
                return await self._pending_handle(verb, client, posture, uuid.uuid4().hex, params_hash)
            try:
                approved = await ask_fn(verb, params, client)
            except MCPApprovalTimeout as timeout:
                # Operator didn't decide within the hold window → degrade the
                # held request to the async handle (same approval_id).
                return await self._pending_handle(verb, client, posture, timeout.approval_id, params_hash)
            if not approved:
                await self._audit(verb, client, posture, "declined", params_hash=params_hash)
                return self._err(403, f"operator declined verb: {verb}", verb)
            # Operator approved the MCP verb → the tool's own policy ASK (if any)
            # is covered by the same approval.
            tool_ask_fn = _always_true

        ctx = VerbContext(
            app=app,
            params=params,
            client=client,
            session_activity_id=session_id or f"mcp:{client.name}",
            ask_fn=tool_ask_fn,
        )
        try:
            data = await CALL_VERBS[verb](ctx)
        except MCPPermissionDenied as exc:
            await self._audit(verb, client, posture, "denied", params_hash=params_hash)
            return self._err(403, f"permission denied: {exc.reason}", verb)
        except MCPVerbError as exc:
            await self._audit(verb, client, posture, f"error_{exc.code}", params_hash=params_hash)
            return self._err(exc.code, exc.message, verb)

        await self._audit(
            verb, client, posture, "ok", params_hash=params_hash, result_summary=_summarize(data)
        )
        return 200, MCPCallResponse(verb=verb, data=data).model_dump()

    async def _pending_handle(
        self, verb: str, client: MCPClient, posture: str, approval_id: str, params_hash: str = ""
    ) -> tuple[int, dict[str, Any]]:
        await self._audit(
            verb, client, posture, f"awaiting_operator:{approval_id}", params_hash=params_hash
        )
        return 202, MCPPendingResponse(verb=verb, approval_id=approval_id).model_dump()

    @staticmethod
    def _err(code: int, message: str, verb: str | None = None) -> tuple[int, dict[str, Any]]:
        return code, MCPErrorResponse(verb=verb, code=code, error=message).model_dump()

    async def _audit(
        self,
        verb: str,
        client: MCPClient,
        posture: str,
        decision: str,
        *,
        params_hash: str = "",
        result_summary: str = "",
    ) -> None:
        await append_mcp_audit_row(
            verb=verb,
            client=client.name,
            trust_tier=client.trust_tier,
            posture=posture,
            decision=decision,
            params_hash=params_hash,
            result_summary=result_summary,
        )


def _hash_params(params: Any) -> str:
    """SHA-256[:16] of the JSON params — a correlation token that never exposes
    the raw arguments in the audit log."""
    try:
        blob = json.dumps(params, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(params)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _summarize(data: Any) -> str:
    text = data if isinstance(data, str) else repr(data)
    return text[:_RESULT_SUMMARY_CAP]


__all__ = ["MCPVerbDispatcher", "MCPAskFn"]
