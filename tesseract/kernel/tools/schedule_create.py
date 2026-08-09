"""schedule_create tool — agent-authored runtime schedule jobs.

Phase 18 Task B. ASK-gated by default (operator-visible config edit).
Calls `SchedulerEngine.add_job_runtime` which persists to `schedule.yaml`
and arms the new job in the live registry. The watcher's debounced
reload (Task A) sees the same write but the diff is empty (registry
already updated) so no spurious toast fires.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.scheduler.config_loader import RetryPolicy

logger = logging.getLogger(__name__)


class ScheduleCreateInput(BaseModel):
    name: str = Field(description="Unique job name (slug-style: 'vault_lint_4h').")
    cadence: str = Field(
        description=(
            "Interval shorthand ('15m', '6h', '1d12h') or 5-field cron "
            "('0 22 * * *'). Picked up by the engine on the next tick."
        )
    )
    handler: str = Field(
        description=(
            "Dotted import path of a BaseJob subclass under "
            "`tesseract.scheduler.tasks.*` (whitelist enforced). Example: "
            "'tesseract.scheduler.tasks.vault_lint.VaultLintJob'."
        )
    )
    enabled: bool = Field(default=True, description="Disable to dry-run before arming.")
    on_failure: str = Field(default="log", description="`log`, `alert`, or `disable`.")
    max_retries: int = Field(default=0, description="Per-fire retry count (0 = run once).")
    backoff_seconds: int = Field(default=0, description="Sleep between retries.")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Handler-specific config dict. Passed to JobContext.config at fire time.",
    )


class ScheduleCreateTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"
    @property
    def name(self) -> str:
        return "schedule_create"

    @property
    def description(self) -> str:
        return (
            "Register a new scheduler job at runtime. Use when the operator asks "
            "for a recurring task ('lint the vault every 6 hours', 'run the daily "
            "writer at 8pm'). The job persists to schedule.yaml and survives "
            "restart. Returns the new job name + cadence.\n\n"
            "For a recurring LLM task that interprets an instruction, optionally "
            "web-searches, filters, and DELIVERS the result to a chat (e.g. "
            "'every morning summarize AI papers and message me', 'weekly "
            "competitor-price check'), use the generic lean handler "
            "'tesseract.scheduler.tasks.scheduled_task.ScheduledTaskJob' with "
            "config {prompt: <the instruction>, channel: 'telegram', chat_ref: "
            "<chat id>, queries?: [<search strings>], title?: <header>}. This is "
            "NOT a mission and needs no approval gate — prefer it over a mission "
            "loop for repetitive fetch/filter/deliver work."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return ScheduleCreateInput

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: ScheduleCreateInput = tool_input  # type: ignore[assignment]
        provider = context.scheduler_provider
        scheduler = provider() if provider is not None else None
        if scheduler is None:
            return ToolResult(
                output="scheduler unavailable in this runtime (REPL or boot failure)",
                is_error=True,
            )
        try:
            cfg = scheduler.add_job_runtime(
                name=inp.name,
                cadence=inp.cadence,
                handler=inp.handler,
                enabled=inp.enabled,
                on_failure=inp.on_failure,
                retry_policy=RetryPolicy(
                    max_retries=inp.max_retries,
                    backoff_seconds=inp.backoff_seconds,
                ),
                config=dict(inp.config),
            )
        except (ValueError, KeyError) as exc:
            return ToolResult(output=f"schedule_create failed: {exc}", is_error=True)
        return ToolResult(
            output=f"job '{cfg.name}' armed (cadence={cfg.cadence}, enabled={cfg.enabled})",
            metadata={
                "name": cfg.name,
                "cadence": cfg.cadence,
                "handler": cfg.handler,
                "enabled": cfg.enabled,
                "on_failure": cfg.on_failure,
            },
        )
