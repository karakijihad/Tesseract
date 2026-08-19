"""lane_attach — re-establish visibility on a lane.

X-4 Session B. AUTO posture — read-only attach. This is the
brain-restart recovery primitive: after a brain restart the brain
reads `<TESSERACT_HOME>/controller/lanes/*/lane.json` to find live
lanes, then calls `lane_attach(lane_id)` per lane to load the snapshot
+ cursor for live tail."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.agent_controller.lanes.tool_support import (
    resolve_lane_manager,
)


class LaneAttachInput(BaseModel):
    lane_id: str = Field(description="Lane id to re-establish visibility on.")


class LaneAttachTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "Re-establish visibility on a live lane, returning a fresh snapshot and cursor."
    use_when: ClassVar[str] = "Use once per lane after a brain restart, before resuming lane_read/lane_send on it."
    not_when: ClassVar[str] = (
        "opening a new lane, which is `lane_open`; probing status without needing a cursor, "
        "which is `lane_status`."
    )

    @property
    def name(self) -> str:
        return "lane_attach"

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneAttachInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: LaneAttachInput = tool_input  # type: ignore[assignment]
        manager = resolve_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_attach unavailable: LaneManager not wired",
                is_error=True,
            )
        try:
            snapshot = await manager.attach(inp.lane_id)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=f"lane_attach failed: {exc}", is_error=True)
        return ToolResult(
            output=(
                f"lane_id={snapshot.lane.lane_id} "
                f"kind={snapshot.lane.kind} "
                f"mode={snapshot.lane.mode} "
                f"lifecycle={snapshot.lane.lifecycle} "
                f"events={len(snapshot.recent_events)} "
                f"next_cursor={snapshot.next_cursor}"
            ),
            metadata={
                "lane": snapshot.lane.model_dump(mode="json"),
                "status": snapshot.status.model_dump(mode="json"),
                "recent_events": [
                    ev.model_dump(mode="json") for ev in snapshot.recent_events
                ],
                "next_cursor": snapshot.next_cursor,
            },
        )
