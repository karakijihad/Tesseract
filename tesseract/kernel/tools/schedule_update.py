"""schedule_update tool — adjust a registered job at runtime.

AU-19. ASK-gated (operator-visible config edit). Accepts any combination
of ``cadence``/``enabled``/``model_role``; routes to the matching
``SchedulerEngine`` setter (``set_cadence``/``set_enabled``/``set_model_role``).
At least one field must be present.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field, model_validator

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class ScheduleUpdateInput(BaseModel):
    name: str = Field(description="Registered job name.")
    cadence: str | None = Field(
        default=None,
        description="New interval shorthand or cron. Leave unset to keep current.",
    )
    enabled: bool | None = Field(
        default=None,
        description="Toggle the job on/off. Leave unset to keep current.",
    )
    summary: str | None = Field(
        default=None,
        description=(
            "Replace what this job says it is for — the line the operator reads "
            "in WHAT-RUNS.md. Only their own rows: what a job the app ships is "
            "for belongs to the app. Leave unset to keep current."
        ),
    )
    model_role: str | None = Field(
        default=None,
        description=(
            "Override the LLM role for handlers that use one. Empty string "
            "clears the override back to the handler default. Leave unset "
            "to keep current."
        ),
    )

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "ScheduleUpdateInput":
        if (
            self.cadence is None
            and self.enabled is None
            and self.model_role is None
            and self.summary is None
        ):
            raise ValueError(
                "schedule_update requires at least one of cadence / enabled / "
                "model_role / summary"
            )
        return self


class ScheduleUpdateTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "time"
    summary: ClassVar[str] = (
        "Changes a registered job's cadence, enabled state, model role or "
        "description in place."
    )
    use_when: ClassVar[str] = (
        "Use to adjust or disable an existing job while keeping it registered, "
        "or to fill in what one of the operator's own rows is for when the "
        "tracker reports it has no summary."
    )
    not_when: ClassVar[str] = (
        "deleting a job entirely, which is `schedule_remove`; firing it once now, which is "
        "`schedule_run`; a one-time reminder, which is `alarm_set`."
    )

    @property
    def name(self) -> str:
        return "schedule_update"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ScheduleUpdateInput

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: ScheduleUpdateInput = tool_input  # type: ignore[assignment]
        provider = context.scheduler_provider
        scheduler = provider() if provider is not None else None
        if scheduler is None:
            return ToolResult(
                output="scheduler unavailable in this runtime (REPL or boot failure)",
                is_error=True,
            )
        applied: dict[str, Any] = {}
        try:
            if inp.cadence is not None:
                scheduler.set_cadence(inp.name, inp.cadence)
                applied["cadence"] = inp.cadence
            if inp.enabled is not None:
                scheduler.set_enabled(inp.name, inp.enabled)
                applied["enabled"] = inp.enabled
            if inp.summary is not None:
                scheduler.set_summary(inp.name, inp.summary)
                applied["summary"] = inp.summary
            if inp.model_role is not None:
                scheduler.set_model_role(inp.name, inp.model_role)
                applied["model_role"] = inp.model_role if inp.model_role else None
        except KeyError:
            return ToolResult(output=f"job {inp.name!r} is not registered", is_error=True)
        except (ValueError, RuntimeError) as exc:
            return ToolResult(output=f"schedule_update failed: {exc}", is_error=True)
        if not applied:
            return ToolResult(output="schedule_update: nothing to apply", is_error=True)
        return ToolResult(
            output=f"job '{inp.name}' updated ({', '.join(f'{k}={v!r}' for k, v in applied.items())})",
            metadata={"name": inp.name, "applied": applied},
        )
