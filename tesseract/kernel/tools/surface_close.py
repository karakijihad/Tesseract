"""surface_close — destroy a surface. AUTO."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.surfaces.store import get_surface_store


class SurfaceCloseInput(BaseModel):
    surface_id: str = Field(description="Target surface id.")


class SurfaceCloseTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "surface_close"

    @property
    def description(self) -> str:
        return "Destroy a surface and remove its card from the canvas."

    @property
    def input_schema(self) -> type[BaseModel]:
        return SurfaceCloseInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SurfaceCloseInput = tool_input  # type: ignore[assignment]
        if not get_surface_store().close(inp.surface_id):
            return ToolResult(
                output=f"surface_close: unknown surface_id {inp.surface_id!r}",
                is_error=True,
            )
        return ToolResult(output=f"closed {inp.surface_id}", metadata={"surface_id": inp.surface_id})
