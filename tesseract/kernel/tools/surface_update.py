"""surface_update — mutate an existing surface's props / title. AUTO."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.surfaces.store import get_surface_store


class SurfaceUpdateInput(BaseModel):
    surface_id: str = Field(description="Target surface id (from surface_create).")
    props: dict[str, Any] | None = Field(
        default=None, description="Props to merge into the surface (shallow merge)."
    )
    title: str | None = Field(default=None, description="New card title.")


class SurfaceUpdateTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "surface_update"

    @property
    def description(self) -> str:
        return "Mutate a surface's props or title. Props merge shallowly into the existing payload."

    @property
    def input_schema(self) -> type[BaseModel]:
        return SurfaceUpdateInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SurfaceUpdateInput = tool_input  # type: ignore[assignment]
        updated = get_surface_store().update(
            inp.surface_id, props=inp.props, title=inp.title
        )
        if updated is None:
            return ToolResult(
                output=f"surface_update: unknown surface_id {inp.surface_id!r}",
                is_error=True,
            )
        return ToolResult(output=f"updated {inp.surface_id}", metadata={"surface_id": inp.surface_id})
