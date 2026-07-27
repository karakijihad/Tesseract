"""lane_named_list — enumerate all named-lane bindings.

X-5 Session A. Read-only (AUTO). Returns every persistent name→lane_id
binding known to the NamedLaneManager. Does not filter orphans (bindings
whose underlying lane is closed) — caller uses lane_status to probe
liveness."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.tars_controller.lanes.tool_support import (
    maybe_await,
    resolve_named_lane_manager,
)


class LaneNamedListInput(BaseModel):
    pass


class LaneNamedListTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "lane_named_list"

    @property
    def description(self) -> str:
        return (
            "List all named-lane bindings (name → lane_id + kind + model). "
            "Does not filter orphans — use lane_status to check liveness. "
            "Read-only."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneNamedListInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        manager = resolve_named_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_named_list unavailable: NamedLaneManager not wired",
                is_error=True,
            )
        try:
            records = await maybe_await(manager.list())
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=f"lane_named_list failed: {exc}", is_error=True)
        if not records:
            return ToolResult(
                output="(no named lanes)", metadata={"count": 0, "records": []}
            )
        lines = [
            f"name={r.name} lane_id={r.lane_id} kind={r.kind} model={r.model}"
            for r in records
        ]
        return ToolResult(
            output="\n".join(lines),
            metadata={
                "count": len(records),
                "records": [r.model_dump(mode="json") for r in records],
            },
        )
