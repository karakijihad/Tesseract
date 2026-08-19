"""os_open_url — hand an http(s) URL to the default browser.

AUTO posture: this is what following a link does. The scheme allowlist is the
control that matters — a `file:` or `ms-settings:` URL reaching ShellExecute
would be a launch wearing a URL's clothes, so those never get here.

Not model-facing. `open` dispatches to it after resolution; nothing chooses it
directly.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.open_verb.native import LaunchRefused, launch_url


class OsOpenUrlInput(BaseModel):
    url: str = Field(description="An http or https URL to open in the default browser.")


class OsOpenUrlTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "showing-the-operator"
    summary: ClassVar[str] = "Internal primitive: open an http(s) URL in the operator's default browser."
    use_when: ClassVar[str] = (
        "Called by `open` after it resolves a target to a URL destination. Not for direct use."
    )
    not_when: ClassVar[str] = (
        "Model callers use `open` — it resolves the target and dispatches here itself."
    )

    @property
    def name(self) -> str:
        return "os_open_url"

    @property
    def input_schema(self) -> type[BaseModel]:
        return OsOpenUrlInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: OsOpenUrlInput = tool_input  # type: ignore[assignment]
        try:
            opened = launch_url(inp.url)
        except LaunchRefused as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"opened {opened}", metadata={"url": opened})
