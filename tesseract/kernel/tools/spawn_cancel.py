"""spawn_cancel — terminate a background spawn.

Phase 4 of the assistant reboot CLI-parity plan. For subprocess delegates
this fires asyncio.Task.cancel() which propagates SIGTERM to the
child via cli_stream's cleanup. For future in-process spawns
(invoke_agent) the inner ChatSession's cancel_event is set.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)


class SpawnCancelInput(BaseModel):
    handle: str = Field(min_length=1, max_length=120)


class SpawnCancelTool(Tool):
    default_posture: ClassVar[str] = "auto"

    risk_class: ClassVar[str] = "autonomous"
    @property
    def name(self) -> str:
        return "spawn_cancel"

    @property
    def description(self) -> str:
        return (
            "Cancel a running background spawn. Marks the handle "
            "cancelled, fires the kind-specific cancel hook (subprocess "
            "SIGTERM for delegate_*), and waits for the task to unwind. "
            "Returns immediately if the handle is already done."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SpawnCancelInput

    def is_concurrency_safe(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SpawnCancelInput)
            else SpawnCancelInput(**tool_input.model_dump())
        )
        registry = getattr(context, "spawns", None)
        if registry is None:
            return ToolResult(
                output="spawn_cancel unavailable: registry not wired",
                is_error=True,
            )
        from tesseract.brain.spawns import cancel_handle, find_handle

        handle = registry.get(inp.handle)
        if handle is None:
            # Same reconnect fallback as spawn_await (M4-p2): a spawn that
            # survived a same-process reconnect lives in the orphaned old
            # registry — resolve it through the process-global index so
            # check/await/cancel stay consistent on the same handle.
            handle = find_handle(inp.handle)
        if handle is None:
            return ToolResult(
                output=f"No spawn with handle={inp.handle!r}.",
                is_error=True,
            )
        cancelled = await cancel_handle(handle)
        if not cancelled:
            return ToolResult(
                output=(
                    f"Spawn {inp.handle} was already done — "
                    f"status={handle.status()}."
                ),
            )
        return ToolResult(
            output=f"Cancelled spawn {inp.handle}.",
            metadata={"handle": inp.handle, "status": "cancelled"},
        )
