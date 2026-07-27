"""Outbound MCP client — connect OUT to curated external MCP servers.

The governed inverse of ``mirror/server/mcp/`` (the inbound server we expose):
each tool a curated server offers becomes an ASK-posture, untrusted-wrapped,
namespaced ``Tool`` in the local registry. Curation-first — only servers listed
in ``config/mcp_servers.yaml`` are ever contacted (security-contract #4).
"""

from __future__ import annotations

from tesseract.mcp_client.manager import MCPClientManager
from tesseract.mcp_client.remote_tool import MCPRemoteTool, build_input_model

__all__ = ["MCPClientManager", "MCPRemoteTool", "build_input_model"]
