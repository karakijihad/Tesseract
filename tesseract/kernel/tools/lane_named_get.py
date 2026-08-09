"""lane_named_get — look up the current lane_id bound to a named lane.

X-5 Session A. Read-only (AUTO). Returns the binding (lane_id, kind,
mode, model, working_dir) or a `bound=false` shape when the name has
no record. Does NOT open a lane on miss — callers wanting an open-on-
miss flow use `lane_named_ensure`."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.agent_controller.lanes.named import (
    InvalidNamedLaneNameError,
)
from tesseract.orchestrator.agent_controller.lanes.tool_support import (
    maybe_await,
    resolve_named_lane_manager,
)


class LaneNamedGetInput(BaseModel):
    name: str = Field(
        description=(
            "Named-lane label (e.g. 'coder/claude'). Must match "
            "[a-z0-9_-]+(/[a-z0-9_-]+)?."
        )
    )


class LaneNamedGetTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "lane_named_get"

    @property
    def description(self) -> str:
        return (
            "Return the current binding for a named lane "
            "(coder/claude, auditor/codex, etc.) — lane_id + kind + "
            "model + working_dir. Returns bound=false when no binding "
            "exists. Read-only; use lane_named_ensure to create."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneNamedGetInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: LaneNamedGetInput = tool_input  # type: ignore[assignment]
        manager = resolve_named_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_named_get unavailable: NamedLaneManager not wired",
                is_error=True,
            )
        try:
            record = await maybe_await(manager.get(inp.name))
        except InvalidNamedLaneNameError as exc:
            return ToolResult(output=f"lane_named_get: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=f"lane_named_get failed: {exc}", is_error=True)
        if record is None:
            return ToolResult(
                output=f"name={inp.name} bound=false",
                metadata={"name": inp.name, "bound": False},
            )
        return ToolResult(
            output=(
                f"name={record.name} lane_id={record.lane_id} "
                f"kind={record.kind} mode={record.mode} model={record.model}"
            ),
            metadata={
                "bound": True,
                **record.model_dump(mode="json"),
            },
        )
