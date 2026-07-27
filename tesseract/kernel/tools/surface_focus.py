"""surface_focus — raise a surface to the front + scroll into view. AUTO."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.surfaces.store import get_surface_store


class SurfaceFocusInput(BaseModel):
    surface_id: str = Field(description="Target surface id.")


class SurfaceFocusTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "surface_focus"

    @property
    def description(self) -> str:
        return "Bring a surface to the front (raise its z) and scroll it into view."

    @property
    def input_schema(self) -> type[BaseModel]:
        return SurfaceFocusInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SurfaceFocusInput = tool_input  # type: ignore[assignment]
        updated = get_surface_store().focus(inp.surface_id)
        if updated is None:
            return ToolResult(
                output=f"surface_focus: unknown surface_id {inp.surface_id!r}",
                is_error=True,
            )
        return ToolResult(output=f"focused {inp.surface_id}", metadata={"surface_id": inp.surface_id})
