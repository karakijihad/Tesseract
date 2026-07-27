"""Bearer-token auth for the MCP server.

Default-deny (mcp-yaml-schema.md): a token matching no configured client is
rejected. The presented ``Authorization: Bearer <token>`` is compared against
every client's secret with :func:`hmac.compare_digest` (constant-time — no
early-exit timing leak) and resolves to the matching :class:`MCPClient`, whose
``trust_tier`` is surfaced on the ``mcp_session`` Activity record.
"""

from __future__ import annotations

import hmac
import logging
import os

from tesseract.config.mcp import MCPClient, MCPConfig

log = logging.getLogger(__name__)

_BEARER_PREFIX = "Bearer "


def extract_bearer(authorization_header: str | None) -> str | None:
    """Return the raw token from an ``Authorization: Bearer <token>`` header,
    or ``None`` when absent/malformed."""
    if not authorization_header or not authorization_header.startswith(_BEARER_PREFIX):
        return None
    token = authorization_header[len(_BEARER_PREFIX):].strip()
    return token or None


def authenticate(config: MCPConfig, authorization_header: str | None) -> MCPClient | None:
    """Resolve the client for a presented bearer token, or ``None`` if the
    token is missing, malformed, or matches no configured client.

    Every configured client is checked (no early return on first mismatch) so
    the comparison cost does not reveal which client matched.
    """
    token = extract_bearer(authorization_header)
    if token is None:
        return None
    presented = token.encode("utf-8")
    matched: MCPClient | None = None
    for client in config.clients:
        secret = os.environ.get(client.token_env)
        if not secret:
            # A client whose secret env is unset can never authenticate. Log
            # once at debug — a misconfigured deployment surfaces as 401.
            log.debug("mcp auth: client %s token env %s unset", client.name, client.token_env)
            continue
        if hmac.compare_digest(presented, secret.encode("utf-8")):
            matched = client
    return matched


__all__ = ["authenticate", "extract_bearer"]
