"""Disposable MCP echo server — the Phase 2 validation target.

A minimal, real, spec-compliant stdio MCP server (built on the official SDK's
``FastMCP``) that the outbound client connects to end-to-end in integration
tests. Referenced by ``config/mcp_servers.yaml::servers.echo_test.command`` as
``python -m tesseract.tests.helpers.mcp_echo_server``. Not wired at boot
(``enabled: false``); tests spawn it explicitly.

Exposes two tools:
  * ``echo``          — returns its ``text`` argument unchanged.
  * ``raise_error``   — returns an ``isError`` result, to exercise the
    client's error mapping.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tesseract-echo-test")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the given text back verbatim."""
    return text


@mcp.tool()
def raise_error(message: str = "boom") -> str:
    """Raise so the client maps it to an isError ToolResult."""
    raise RuntimeError(message)


if __name__ == "__main__":
    mcp.run(transport="stdio")
