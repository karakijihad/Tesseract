"""schedule_run tool — fire a registered job immediately, off-schedule.

AU-19. ASK-gated. Wraps ``SchedulerEngine.run_now``. The job's ``enabled``
flag is ignored — manual triggers work on disabled rows too, which is the
whole point of "run now".
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class ScheduleRunInput(BaseModel):
    name: str = Field(description="Registered job name to fire now.")


class ScheduleRunTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    @property
    def name(self) -> str:
        return "schedule_run"

    @property
    def description(self) -> str:
        return (
            "Fire a registered scheduler job immediately, off-schedule. "
            "ASK-gated. Same execution path as the tick loop — produces "
            "the standard run record + broadcast envelope."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return ScheduleRunInput

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: ScheduleRunInput = tool_input  # type: ignore[assignment]
        provider = context.scheduler_provider
        scheduler = provider() if provider is not None else None
        if scheduler is None:
            return ToolResult(
                output="scheduler unavailable in this runtime (REPL or boot failure)",
                is_error=True,
            )
        try:
            result = await scheduler.run_now(inp.name)
        except KeyError:
            return ToolResult(output=f"job {inp.name!r} is not registered", is_error=True)
        return ToolResult(
            output=(
                f"job '{inp.name}' fired (ok={result.ok}, detail={result.detail!r}, "
                f"duration_ms={result.duration_ms:.0f})"
            ),
            metadata={
                "name": inp.name,
                "run_id": result.run_id,
                "ok": result.ok,
                "detail": result.detail,
                "duration_ms": result.duration_ms,
            },
            is_error=not result.ok,
        )
