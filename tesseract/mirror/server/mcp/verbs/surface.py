"""surface.* MCP verbs (P3) — route to the surface_* kernel tools, which use the
process-global surface store (no ToolContext provider needed). Verb ``spawn``
maps to the ``surface_create`` tool."""

from __future__ import annotations

from tesseract.kernel.tools.surface_close import SurfaceCloseInput
from tesseract.kernel.tools.surface_create import SurfaceCreateInput
from tesseract.kernel.tools.surface_focus import SurfaceFocusInput
from tesseract.kernel.tools.surface_update import SurfaceUpdateInput
from tesseract.mirror.server.mcp.verbs._base import make_tool_verb

surface_spawn = make_tool_verb("surface_create", SurfaceCreateInput)
surface_update = make_tool_verb("surface_update", SurfaceUpdateInput)
surface_focus = make_tool_verb("surface_focus", SurfaceFocusInput)
surface_close = make_tool_verb("surface_close", SurfaceCloseInput)

__all__ = ["surface_spawn", "surface_update", "surface_focus", "surface_close"]
