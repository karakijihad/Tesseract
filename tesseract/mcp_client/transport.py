"""Transport openers for the outbound MCP client.

Wraps the official SDK's two client transports behind one uniform async
context manager that yields the ``(read, write)`` stream pair every
``ClientSession`` needs:

  * ``stdio``  → ``stdio_client(StdioServerParameters(...))`` (2-tuple)
  * ``http``   → ``streamablehttp_client(url, ...)`` (3-tuple; the third
    element is a ``get_session_id`` callable we don't need at this layer)

Secrets are never read from config — only env-var NAMES (validated by
``config/mcp_client.py``); this layer resolves them from the process
environment at connect time. API verified against installed ``mcp`` 1.27.0
(2026-07-17).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from tesseract.config.mcp_client import MCPClientDefaults, MCPServerSpec

log = logging.getLogger(__name__)


def _stdio_env(spec: MCPServerSpec) -> dict[str, str]:
    """Default child environment plus the operator-listed passthrough vars.

    Start from the SDK's minimal safe env (PATH/HOME/etc.) so the child can
    actually find its interpreter, then layer only the explicitly-listed
    ``env_passthrough`` names present in the current process environment.
    """
    env = dict(get_default_environment())
    for name in spec.env_passthrough:
        val = os.environ.get(name)
        if val is not None:
            env[name] = val
        else:
            log.warning(
                "mcp_client: server %s env_passthrough name %r not set in environment",
                spec.name,
                name,
            )
    return env


def _http_headers(spec: MCPServerSpec) -> dict[str, str] | None:
    if not spec.auth_token_env:
        return None
    token = os.environ.get(spec.auth_token_env)
    if not token:
        log.warning(
            "mcp_client: server %s auth_token_env %r not set — connecting without bearer",
            spec.name,
            spec.auth_token_env,
        )
        return None
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def open_transport(
    spec: MCPServerSpec, defaults: MCPClientDefaults
) -> AsyncIterator[tuple]:
    """Yield ``(read_stream, write_stream)`` for ``spec``'s transport.

    Must be entered and exited within a single task (the SDK transports use
    anyio task groups internally); ``manager.py`` honours that by owning each
    connection in a dedicated per-server task.
    """
    if spec.transport == "stdio":
        params = StdioServerParameters(
            command=spec.command[0],
            args=list(spec.command[1:]),
            env=_stdio_env(spec),
        )
        async with stdio_client(params) as (read, write):
            yield read, write
    elif spec.transport == "http":
        if not spec.url:
            raise RuntimeError(f"mcp_client: server {spec.name} http transport missing url")
        async with streamablehttp_client(
            spec.url,
            headers=_http_headers(spec),
            timeout=defaults.connect_timeout_s,
        ) as (read, write, _get_session_id):
            yield read, write
    else:  # pragma: no cover - config loader already validates the set
        raise RuntimeError(
            f"mcp_client: server {spec.name} unknown transport {spec.transport!r}"
        )


__all__ = ["open_transport"]
