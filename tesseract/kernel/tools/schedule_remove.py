"""schedule_remove tool — operator-or-TARS removal of a scheduler job.

Phase 18 Task B. ASK-gated. Calls `SchedulerEngine.remove_job_runtime`
which trims `schedule.yaml` and the live registry. Built-in jobs
shipped in the repo (daily_writer, vault_lint, etc.) can be removed
through this path — operators who want to keep them but stop firing
should `set_enabled(false)` via the existing /schedule-disable command
instead.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


class ScheduleRemoveInput(BaseModel):
    name: str = Field(description="Job name to remove. Must already be registered.")


class ScheduleRemoveTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"
    @property
    def name(self) -> str:
        return "schedule_remove"

    @property
    def description(self) -> str:
        return (
            "Remove a scheduler job by name. Use when the operator asks to "
            "delete a job entirely. To keep the job but stop firing, use "
            "/schedule-disable instead. Persists to schedule.yaml."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return ScheduleRemoveInput

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: ScheduleRemoveInput = tool_input  # type: ignore[assignment]
        provider = context.scheduler_provider
        scheduler = provider() if provider is not None else None
        if scheduler is None:
            return ToolResult(
                output="scheduler unavailable in this runtime (REPL or boot failure)",
                is_error=True,
            )
        try:
            cfg = scheduler.remove_job_runtime(inp.name)
        except KeyError:
            return ToolResult(output=f"job {inp.name!r} is not registered", is_error=True)
        except Exception as exc:
            return ToolResult(output=f"schedule_remove failed: {exc}", is_error=True)
        return ToolResult(
            output=f"job '{cfg.name}' removed",
            metadata={"name": cfg.name, "cadence": cfg.cadence, "handler": cfg.handler},
        )
