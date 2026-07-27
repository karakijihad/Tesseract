"""lane_list — enumerate live (non-archived) lanes."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.tars_controller.lanes.tool_support import (
    maybe_await,
    resolve_lane_manager,
)


class LaneListInput(BaseModel):
    pass


class LaneListTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "lane_list"

    @property
    def description(self) -> str:
        return (
            "List every live (non-archived) lane the controller hosts. "
            "Use lane_status / lane_attach to drill in. Read-only."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneListInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        manager = resolve_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_list unavailable: LaneManager not wired",
                is_error=True,
            )
        try:
            ids = await maybe_await(manager.list_ids())
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=f"lane_list failed: {exc}", is_error=True)
        if not ids:
            return ToolResult(output="(no lanes)", metadata={"count": 0, "ids": []})
        return ToolResult(
            output="\n".join(ids),
            metadata={"count": len(ids), "ids": ids},
        )
