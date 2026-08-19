"""surface_bind_session — attach a runtime session (lane / channel)
to a surface for live updates. AUTO.

Y-2 ships the binding mechanism; the rich live renderers that consume the
binding land later (lane card in CV-1, channel card in P4-3)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.surfaces.store import get_surface_store


class SurfaceBindSessionInput(BaseModel):
    surface_id: str = Field(description="Target surface id.")
    session_kind: str = Field(description="Session kind: 'lane' | 'channel'.")
    session_id: str = Field(description="Id of the session to bind for live updates.")


class SurfaceBindSessionTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "showing-the-operator"
    summary: ClassVar[str] = (
        "Attach a lane or channel session to a surface so its card streams live updates."
    )
    use_when: ClassVar[str] = (
        "A surface should reflect a running session's activity as it happens, "
        "rather than a static snapshot."
    )
    not_when: ClassVar[str] = (
        "`surface_update` for a one-off content change; this is for ongoing "
        "live streaming from a session."
    )

    @property
    def name(self) -> str:
        return "surface_bind_session"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SurfaceBindSessionInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SurfaceBindSessionInput = tool_input  # type: ignore[assignment]
        updated = get_surface_store().bind_session(
            inp.surface_id, session_kind=inp.session_kind, session_id=inp.session_id
        )
        if updated is None:
            return ToolResult(
                output=f"surface_bind_session: unknown surface_id {inp.surface_id!r}",
                is_error=True,
            )
        return ToolResult(
            output=f"bound {inp.session_kind}:{inp.session_id} to {inp.surface_id}",
            metadata={"surface_id": inp.surface_id},
        )
