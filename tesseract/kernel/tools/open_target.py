"""open — the single verb for showing anything.

A URL, a file, a folder, an application, or a search phrase. It renders in the
cockpit when it can and goes to the application that owns it when it can't, and
the caller never picks which. The result says which way it went.

AUTO posture, and it gates nothing itself: it resolves, then dispatches to
`surface_create` (auto), `os_open_url` (auto) or `os_launch` (ask) through the
normal permission gateway. The gate lives on the primitive, where the risk is.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.config.open_verb import load_open_config
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.open_verb.execute import ExecutionUnavailable, execute
from tesseract.orchestrator.open_verb.resolve import (
    AmbiguousTarget,
    RefusedTarget,
    resolve,
)


class OpenInput(BaseModel):
    target: str = Field(
        description=(
            "What to open. A URL (https://…), a bare domain (bbc.co.uk), a "
            "local file or folder path, a configured application name, or a "
            "search phrase. Renders in the cockpit when possible, otherwise "
            "opens in the application that owns it."
        )
    )
    view: str = Field(
        default="tars", description="Canvas view a cockpit card belongs to."
    )


class OpenTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "open"

    @property
    def description(self) -> str:
        return (
            "Open anything: a URL, a file, a folder, an application, or a "
            "search. Renders it in the cockpit when it can and opens it in the "
            "owning application when it can't. Returns what it did."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return OpenInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: OpenInput = tool_input  # type: ignore[assignment]
        try:
            config = load_open_config()
            resolution = await resolve(inp.target, config=config)
            outcome = await execute(resolution, context, view=inp.view)
        except (AmbiguousTarget, RefusedTarget) as exc:
            return ToolResult(output=str(exc), is_error=True)
        except PermissionError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except ExecutionUnavailable as exc:
            return ToolResult(output=f"open unavailable: {exc}", is_error=True)

        return ToolResult(
            output=outcome.reason,
            is_error=outcome.is_error,
            metadata={
                "destination": outcome.destination,
                "resolved_kind": outcome.resolved_kind,
                "canonical_target": outcome.canonical_target,
                "handler": outcome.handler,
                "surface_id": outcome.surface_id,
            },
        )
