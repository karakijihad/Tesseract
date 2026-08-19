"""schedule_list tool — read-only enumeration of registered scheduler jobs.

AU-19. Agent calls this to see what's already scheduled before proposing
a new job. Returns name, cadence, enabled, last_fired_at, circuit_broken,
and the resolved model_role for LLM-using handlers. Pure read — no
mutation, no ASK.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class ScheduleListInput(BaseModel):
    enabled_only: bool = Field(
        default=False,
        description="Filter to enabled jobs only. Default returns every registered row.",
    )


class ScheduleListTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "time"
    summary: ClassVar[str] = "Lists registered scheduler jobs with cadence, enabled state, and last-fire time."
    use_when: ClassVar[str] = (
        "Use before creating a job to avoid duplicates, or to check an existing job's runtime state."
    )
    not_when: ClassVar[str] = "pending alarms, which is `alarm_list`."

    @property
    def name(self) -> str:
        return "schedule_list"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ScheduleListInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: ScheduleListInput = tool_input  # type: ignore[assignment]
        provider = context.scheduler_provider
        scheduler = provider() if provider is not None else None
        if scheduler is None:
            return ToolResult(
                output="scheduler unavailable in this runtime (REPL or boot failure)",
                is_error=True,
            )
        jobs: list[dict[str, Any]] = []
        for cfg in scheduler.configs:
            state = scheduler.runtime_state(cfg.name)
            if inp.enabled_only and not state.get("enabled"):
                continue
            jobs.append(
                {
                    "name": state["name"],
                    "cadence": state["cadence"],
                    "enabled": state["enabled"],
                    "circuit_broken": state["circuit_broken"],
                    "consecutive_failures": state["consecutive_failures"],
                    "last_fired_at": state["last_fired_at"],
                    "uses_llm": state["uses_llm"],
                    "effective_model_role": state["effective_model_role"],
                    "handler": cfg.handler,
                }
            )
        return ToolResult(
            output=f"{len(jobs)} job(s) registered",
            metadata={"jobs": jobs},
        )
