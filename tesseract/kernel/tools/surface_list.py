"""surface_list — enumerate the surfaces on a canvas view. AUTO, read-only.

The counterpart the flailing-recovery loop was missing: `surface_create`
returns only a single id, so without this a caller can't see what it has
already spawned (and ends up spamming duplicates / renaming titles instead of
closing). Returns a compact id/type/title/mode listing, z-ordered."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.surfaces.store import get_surface_store


class SurfaceListInput(BaseModel):
    view: str = Field(default="tars", description="Canvas view to list (e.g. 'tars').")


class SurfaceListTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "surface_list"

    @property
    def description(self) -> str:
        return (
            "List the surfaces on a canvas view (id, type, title, mode), "
            "z-ordered. Read-only — use before spawning duplicates or to find "
            "the surface_id to surface_close/surface_update."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SurfaceListInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SurfaceListInput = tool_input  # type: ignore[assignment]
        rows = get_surface_store().list_for_view(inp.view)
        if not rows:
            return ToolResult(output=f"no surfaces on view {inp.view!r}", metadata={"count": 0})
        lines = [
            f"{r['id']} | {r['type']} | {r.get('title') or '-'} | {r.get('mode') or 'embedded'}"
            for r in rows
        ]
        return ToolResult(
            output=f"{len(rows)} surface(s) on {inp.view!r}:\n" + "\n".join(lines),
            metadata={"count": len(rows), "view": inp.view},
        )
