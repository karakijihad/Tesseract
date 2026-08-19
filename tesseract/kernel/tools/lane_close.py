"""lane_close — terminate a lane.

X-4 Session B. ASK-gated — closing a lane terminates the underlying
CLI subprocess and archives the lane directory. Reason strings should
match the contract's enum: `operator_close`, `timeout`,
`error_unrecoverable`, `mission_complete`, `shutdown`."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.agent_controller.lanes.tool_support import (
    resolve_lane_manager,
)


class LaneCloseInput(BaseModel):
    lane_id: str = Field(description="Lane id to terminate.")
    reason: str = Field(
        default="operator_close",
        description=(
            "Reason string recorded in the lane's archive. Conventional "
            "values: operator_close, timeout, error_unrecoverable, "
            "mission_complete, shutdown."
        ),
    )


class LaneCloseTool(Tool):
    default_posture = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "Terminate a lane's CLI subprocess and archive its on-disk record."
    use_when: ClassVar[str] = "Use when a lane's work is done and its process and history should stop existing."
    not_when: ClassVar[str] = (
        "pausing without ending the process — no lane tool does that; `lane_read`/`lane_status` "
        "just stop being polled instead."
    )

    @property
    def name(self) -> str:
        return "lane_close"

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneCloseInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: LaneCloseInput = tool_input  # type: ignore[assignment]
        manager = resolve_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_close unavailable: LaneManager not wired",
                is_error=True,
            )
        try:
            result = await manager.close(inp.lane_id, inp.reason)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=f"lane_close failed: {exc}", is_error=True)
        return ToolResult(
            output=(
                f"lane_id={inp.lane_id} status={result['final_status']} "
                f"archive={result.get('archive_dir', '')}"
            ),
            metadata={
                "lane_id": inp.lane_id,
                "reason": inp.reason,
                **result,
            },
        )
