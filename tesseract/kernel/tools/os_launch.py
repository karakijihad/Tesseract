"""os_launch — hand a local file or folder to the application that owns it.

ASK posture, and the only gate in the `open` design. This is process launch,
not a surface: ShellExecute starts whatever program is registered for the type,
and `bash_security` never sees it. The guard chain in
`orchestrator/open_verb/native.py` bounds *which* object can be handed over;
this posture bounds *when*.

Not model-facing. `open` dispatches to it after resolution.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.config.open_verb import load_open_config
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.open_verb.native import (
    LaunchRefused,
    LaunchUnsupported,
    launch_app,
    launch_directory,
    launch_path,
    reject_network_path,
)


class OsLaunchInput(BaseModel):
    path: str | None = Field(
        default=None,
        description="An existing local file or folder to open in its owning application.",
    )
    app: str | None = Field(
        default=None,
        description=(
            "A configured application identity from open_verb.yaml::apps. "
            "Mutually exclusive with `path` — an application is an executable, "
            "and the file path refuses those absolutely."
        ),
    )


class OsLaunchTool(Tool):
    default_posture = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    @property
    def name(self) -> str:
        return "os_launch"

    @property
    def description(self) -> str:
        return (
            "Open a local file or folder in the application that owns it. "
            "Internal primitive — callers use `open`."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return OsLaunchInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: OsLaunchInput = tool_input  # type: ignore[assignment]
        config = load_open_config()
        if bool(inp.path) == bool(inp.app):
            return ToolResult(
                output="os_launch takes exactly one of `path` or `app`",
                is_error=True,
            )
        try:
            if inp.app is not None:
                opened = launch_app(
                    inp.app, allowed=frozenset(config.apps.values())
                )
            else:
                # The string checks run BEFORE the stat that picks file vs
                # folder. `is_dir()` on a UNC path opens an SMB connection and
                # Windows hands over NTLM credentials, so deciding the branch
                # first would authenticate before any guard had a say.
                reject_network_path(str(inp.path))
                candidate = Path(str(inp.path)).expanduser()
                if candidate.is_dir():
                    opened = launch_directory(str(inp.path))
                else:
                    opened = launch_path(
                        str(inp.path), allowed_extensions=config.launch_extensions
                    )
        except (LaunchRefused, LaunchUnsupported) as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"opened {opened}", metadata={"target": str(opened)})
