"""surface_create — spawn a canvas surface (Surface Protocol v1).

AUTO posture: canvas mutations are not security-sensitive at the tool layer
(security lives at the renderer / data-fetch layer). Returns the surface_id.
``mode`` is a rendering hint only. ``external`` does NOT open anything on
the OS — the canvas simply declines to draw the card (`SurfaceLayer.tsx`), so
a descriptor written with it is invisible and inert. Opening something outside
the cockpit is `open`'s job, via `os_open_url` / `os_launch`."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.surfaces.store import get_surface_store


class SurfaceCreateInput(BaseModel):
    type: str = Field(
        description=(
            "Surface type — drives the renderer. One of the protocol "
            "vocabulary: folder, file, url, app, terminal, browser, document, "
            "media, iframe, webview, lane, channel, mission, "
            "memory, approval, code, markdown, html, form, tree, timeline, "
            "graph, image, diff, pdf, video, audio, table. Unknown types "
            "render as a JSON-dump fallback card."
        )
    )
    view: str = Field(description="Canvas view the surface belongs to (e.g. 'tars').")
    props: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Renderer payload — key depends on type: html {html} (a full "
            "document or fragment; text/content/body also accepted), "
            "markdown {text}, code {text,language}, file {text}, "
            "folder {root}, webview/browser/url/iframe {url} "
            "(http(s) only — file:// URLs are blocked by the browser), "
            "image {url}."
        ),
    )
    title: str | None = Field(default=None, description="Card title.")
    position: dict[str, float] | None = Field(
        default=None, description="{x,y} container coords. Defaults to a cascade offset."
    )
    size: dict[str, float] | None = Field(
        default=None, description="{w,h} pixels. Defaults to 640x460."
    )
    mode: str = Field(
        default="embedded",
        description=(
            "embedded (canvas card) | external (not drawn — NOT an OS open; "
            "use `open` for that) | background (no visual)."
        ),
    )
    replaces: str | None = Field(
        default=None,
        description=(
            "surface_id this new surface supersedes — it's closed automatically "
            "once the new one is created. Use it on a fallback so the dead card "
            "doesn't pile up when one supersedes another. Best-effort; an "
            "unknown/already-closed id is ignored."
        ),
    )


class SurfaceCreateTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "surface_create"

    @property
    def description(self) -> str:
        return (
            "Author a surface from content you generated — a live html app, a "
            "chart, markdown or code you wrote. Returns surface_id. To SHOW "
            "something that already exists (a url, a file, a folder, an app), "
            "use `open` instead: it resolves the target and picks the type "
            "itself. Use surface_update to mutate a card, surface_focus to "
            "raise it, surface_close to dismiss it."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SurfaceCreateInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SurfaceCreateInput = tool_input  # type: ignore[assignment]
        store = get_surface_store()
        try:
            sid = store.create(
                type=inp.type,
                view=inp.view,
                props=inp.props,
                title=inp.title,
                position=inp.position,
                size=inp.size,
                mode=inp.mode,
            )
        except Exception as exc:  # noqa: BLE001 — surface as clean tool error
            return ToolResult(output=f"surface_create failed: {exc}", is_error=True)
        # Close the superseded surface only AFTER the replacement exists, so a
        # failed create never leaves the operator with neither card. Best-effort
        # — an unknown/already-closed id must not fail the create.
        replaced: str | None = None
        if inp.replaces:
            try:
                if store.close(inp.replaces):
                    replaced = inp.replaces
            except Exception:  # noqa: BLE001 — replacement already succeeded
                pass
        return ToolResult(
            output=f"surface_id={sid}",
            metadata={"surface_id": sid, "type": inp.type, "view": inp.view, "replaced": replaced},
        )
