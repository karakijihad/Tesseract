"""surface_highlight — visual emphasis (pulse / glow). AUTO.

Auto-fades on the renderer unless ``persistent`` is set, in which case the
card stays lit until a subsequent non-persistent highlight or close."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.surfaces.store import get_surface_store


class SurfaceHighlightInput(BaseModel):
    surface_id: str = Field(description="Target surface id.")
    persistent: bool = Field(
        default=False,
        description="Keep the highlight lit (vs. auto-fade after a beat).",
    )


class SurfaceHighlightTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "showing-the-operator"
    summary: ClassVar[str] = "Pulse or glow a surface briefly to draw the eye to it."
    use_when: ClassVar[str] = (
        "Something changed on a card and the operator's attention should land "
        "there without sending a message. Auto-fades unless `persistent=true`."
    )
    not_when: ClassVar[str] = (
        "`surface_focus` to actually raise/scroll the card into view — a "
        "highlight alone does not move it."
    )

    @property
    def name(self) -> str:
        return "surface_highlight"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SurfaceHighlightInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SurfaceHighlightInput = tool_input  # type: ignore[assignment]
        if not get_surface_store().highlight(inp.surface_id, persistent=inp.persistent):
            return ToolResult(
                output=f"surface_highlight: unknown surface_id {inp.surface_id!r}",
                is_error=True,
            )
        return ToolResult(
            output=f"highlighted {inp.surface_id}",
            metadata={"surface_id": inp.surface_id, "persistent": inp.persistent},
        )
