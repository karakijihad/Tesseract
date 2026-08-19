"""lane_read — pull events from a lane since the last cursor.

X-4 Session B. AUTO posture — read-only; cannot mutate lane state.
The cursor is an opaque string returned by the previous read; pass
`null` / empty for the first read or a fresh `lane_attach`."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.agent_controller.lanes.tool_support import (
    maybe_await,
    resolve_lane_manager,
)


class LaneReadInput(BaseModel):
    lane_id: str = Field(description="Lane id to tail.")
    since_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque cursor returned by the previous read. None / empty "
            "to read from the beginning of the lane's events.jsonl."
        ),
    )


class LaneReadTool(Tool):
    default_posture = "auto"
    risk_class: ClassVar[str] = "autonomous"

    # A CLI's reply is whatever it read out of a repository, a web page, or
    # a compromised model — untrusted by origin, however trusted the tool
    # that fetched it. Set here so the foreground result and `spawn_await`'s
    # retrieval get the same envelope the background completion delivery
    # applies in `chat.py::_format_spawn_completion`; wrapping only the one
    # path left the other two open.
    untrusted_source: ClassVar[bool] = True

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = "Pull a lane's events since a cursor — read-only tail, sends nothing."
    use_when: ClassVar[str] = (
        "Use to catch up on what a lane produced after a prior lane_send, or to keep polling a long turn."
    )
    not_when: ClassVar[str] = (
        "sending a new message, which is `lane_send`; sending and waiting for the reply in one "
        "call, which is `lane_turn`."
    )

    @property
    def name(self) -> str:
        return "lane_read"

    @property
    def input_schema(self) -> type[BaseModel]:
        return LaneReadInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: LaneReadInput = tool_input  # type: ignore[assignment]
        manager = resolve_lane_manager(context)
        if manager is None:
            return ToolResult(
                output="lane_read unavailable: LaneManager not wired",
                is_error=True,
            )
        try:
            events, next_cursor = await maybe_await(manager.read(inp.lane_id, inp.since_cursor))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=f"lane_read failed: {exc}", is_error=True)
        # Compact textual summary: one event per line, payload pruned to
        # ~120 chars so the chat brain sees a useful summary without
        # blowing context. Full payload available in metadata.
        lines: list[str] = []
        for ev in events:
            payload_str = str(ev.payload)
            if len(payload_str) > 120:
                payload_str = payload_str[:117] + "..."
            lines.append(f"{ev.cursor}\t{ev.kind}\t{payload_str}")
        return ToolResult(
            output="\n".join(lines) if lines else "(no new events)",
            metadata={
                "lane_id": inp.lane_id,
                "count": len(events),
                "next_cursor": next_cursor,
                "events": [ev.model_dump(mode="json") for ev in events],
            },
        )
