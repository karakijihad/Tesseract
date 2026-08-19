"""lane_status — fast read-only status probe for a lane."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.agent_controller.lanes.tool_support import (
    maybe_await,
    resolve_lane_manager,
)


class LaneStatusInput(BaseModel):
    lane_id: str = Field(description="Lane id to probe.")


class LaneStatusTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "Fast read-only probe of one lane's alive/busy/queue_depth state."
    use_when: ClassVar[str] = "Use to check whether a specific lane is free before sending, or confirm it is still alive."
    not_when: ClassVar[str] = (
        "enumerating lanes or bindings, which is `lane_list`/`lane_named_list`; resolving a "
        "name's binding, which is `lane_named_get`."
    )

    @property
    def name(self) -> str:
        return "lane_status"

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneStatusInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: LaneStatusInput = tool_input  # type: ignore[assignment]
        manager = resolve_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_status unavailable: LaneManager not wired",
                is_error=True,
            )
        try:
            status = await maybe_await(manager.status(inp.lane_id))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=f"lane_status failed: {exc}", is_error=True)
        return ToolResult(
            output=(
                f"alive={status.alive} busy={status.busy} "
                f"queue_depth={status.queue_depth} "
                f"lifecycle={status.lifecycle} "
                f"last_activity={status.last_activity_utc}"
            ),
            metadata=status.model_dump(mode="json"),
        )
