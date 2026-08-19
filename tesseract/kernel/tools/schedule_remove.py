"""schedule_remove tool — operator-or-the assistant removal of a scheduler job.

Phase 18 Task B. ASK-gated. Calls `SchedulerEngine.remove_job_runtime`
which trims the operator's `schedule.yaml` and the live registry.

Removes the operator's own jobs only. A job the app ships is declared in
the sealed app tree, so deleting the operator's row would drop their
overrides and the job would return on the next start — the call is refused
with that reason, and disabling is the way to stop one firing.
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

    group: ClassVar[str] = "time"
    summary: ClassVar[str] = "Deletes one of the operator's scheduler jobs entirely."
    use_when: ClassVar[str] = "Use when the operator wants a job gone for good, not paused."
    not_when: ClassVar[str] = (
        "disabling a job while keeping it registered, which is `schedule_update`; a job the app "
        "ships, which cannot be removed and only disabled; canceling a one-time reminder, which "
        "is `alarm_cancel`."
    )

    @property
    def name(self) -> str:
        return "schedule_remove"

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
