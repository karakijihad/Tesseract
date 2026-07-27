"""MCP server subpackage — TESSERACT exposed as a spec-compliant local MCP
server (Streamable-HTTP transport) embedded in the Mirror backend
(mcp-control-plane P2 scaffold → P4 real protocol)."""

from __future__ import annotations

from tesseract.mirror.server.mcp.server import MCPServer

__all__ = ["MCPServer"]
