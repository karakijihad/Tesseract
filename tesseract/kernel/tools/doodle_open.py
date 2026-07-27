"""doodle_open — spawn the canonical Doodle canvas as an html surface.

Thin wrapper over the SurfaceStore: reads the committed Doodle asset
(``mirror/public/doodle.html``) and renders it via the ``html`` surface type
(srcDoc, sandboxed iframe — see HtmlRenderer). Reproducible ``load Doodle``
without pumping the markup through the model each turn.

AUTO posture: a canvas mutation carries no tool-layer security weight."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.surfaces.store import get_surface_store
from tesseract.paths import TESSERACT_DIR

_DOODLE_ASSET = TESSERACT_DIR / "mirror" / "public" / "doodle.html"


class DoodleOpenInput(BaseModel):
    view: str = Field(default="tars", description="Canvas view to spawn on.")
    title: str = Field(default="Doodle", description="Card title.")


class DoodleOpenTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "doodle_open"

    @property
    def description(self) -> str:
        return (
            "Open the Doodle drawing canvas (pen/undo/PNG-export sketch tool) as "
            "an html surface on a view. Call this for operator requests like "
            "'load doodle', 'open doodle', or 'launch the doodle'. Returns "
            "surface_id."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return DoodleOpenInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: DoodleOpenInput = tool_input  # type: ignore[assignment]
        try:
            html = _DOODLE_ASSET.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                output=f"doodle_open failed: asset unreadable: {exc}", is_error=True
            )
        try:
            sid = get_surface_store().create(
                type="html",
                view=inp.view,
                props={"html": html},
                title=inp.title,
                size={"w": 900, "h": 600},
            )
        except Exception as exc:  # noqa: BLE001 — surface as clean tool error
            return ToolResult(output=f"doodle_open failed: {exc}", is_error=True)
        return ToolResult(
            output=f"surface_id={sid}",
            metadata={"surface_id": sid, "type": "html", "view": inp.view},
        )
