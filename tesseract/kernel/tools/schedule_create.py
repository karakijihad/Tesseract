"""schedule_create tool — agent-authored runtime schedule jobs.

ASK-gated by default (operator-visible config edit).
Calls `SchedulerEngine.add_job_runtime` which persists to `schedule.yaml`
and arms the new job in the live registry. The watcher's debounced
reload sees the same write but the diff is empty (registry
already updated) so no spurious toast fires.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.scheduler.config_loader import RetryPolicy

logger = logging.getLogger(__name__)


class ScheduleCreateInput(BaseModel):
    name: str = Field(description="Unique job name (slug-style: 'reading_list_weekly').")
    cadence: str = Field(
        description=(
            "Interval shorthand ('15m', '6h', '1d12h') or 5-field cron "
            "('0 22 * * *'). Picked up by the engine on the next tick."
        )
    )
    handler: str = Field(
        description=(
            "Dotted import path of a BaseJob subclass under "
            "`tesseract.scheduler.tasks.*` (whitelist enforced). Two generic "
            "ones cover almost everything, so a recurring task rarely needs a "
            "job module written for it. To run a described task, use "
            "'tesseract.scheduler.tasks.scheduled_task.ScheduledTaskJob'. To "
            "run a tool on a cadence, whether it is one the app ships, one of "
            "the operator's own, or `invoke_agent`, use "
            "'tesseract.scheduler.tasks.tool_call.ToolCallJob'."
        )
    )
    summary: str = Field(
        description=(
            "One line saying what this job is for, in the operator's words. It "
            "is what they read in WHAT-RUNS.md and in Managed system. Required: a "
            "row nobody can explain is a row nobody can decide to keep."
        )
    )
    enabled: bool = Field(
        default=False,
        description=(
            "Whether the job actually runs. It starts off, on purpose: making "
            "a row is describing a plan and arming it is agreeing to it, and "
            "those are two different decisions. The operator turns it on when "
            "they are happy with it, or asks you to."
        ),
    )
    on_failure: str = Field(default="log", description="`log`, `alert`, or `disable`.")
    max_retries: int = Field(default=0, description="Per-fire retry count (0 = run once).")
    backoff_seconds: int = Field(default=0, description="Sleep between retries.")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Handler-specific config, passed to the job at fire time. For "
            "ScheduledTaskJob: `prompt` is what to do each time and is "
            "required, `title` is what to call the row in the message it "
            "sends, and `queries` is a list of web searches to run first for "
            "fresh grounding, omitted for a task that needs none. For "
            "ToolCallJob: `tool` is the registered tool name and is required, "
            "`args` is what to call it with, and `title` names the row. A "
            "scheduled tool has to be one that already runs without asking, "
            "because nobody is there to answer at 3am, so a tool set to ask "
            "is refused here and the operator changes it in permissions.yaml "
            "if they want it running on its own. Do not put "
            "a channel or a chat id in here: where the result goes is the "
            "`delivery` field, or the routing you already have."
        ),
    )
    delivery: list[str] | None = Field(
        default=None,
        description=(
            "Channels this row's messages go to, overriding `routing.yaml` for "
            "this row only. Leave it unset unless the operator said where they "
            "want it: unset means the message goes wherever that kind already "
            "goes, which is what they will expect. An empty list means it goes "
            "nowhere off this machine and stays on the screen."
        ),
    )


class ScheduleCreateTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "time"
    summary: ClassVar[str] = "Registers a new job that recurs on its configured cadence, persisted to disk."
    use_when: ClassVar[str] = "Use when the operator wants a task to run repeatedly going forward, not once."
    not_when: ClassVar[str] = (
        "a single one-time reminder, which is `alarm_set`; changing an existing job is "
        "`schedule_update`."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "job"
    recovery_behaviour: ClassVar[str] = "idempotent"

    @property
    def name(self) -> str:
        return "schedule_create"

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
                summary=inp.summary,
                enabled=inp.enabled,
                on_failure=inp.on_failure,
                retry_policy=RetryPolicy(
                    max_retries=inp.max_retries,
                    backoff_seconds=inp.backoff_seconds,
                ),
                config=dict(inp.config),
                delivery=(
                    None if inp.delivery is None else list(inp.delivery)
                ),
            )
        except (ValueError, KeyError) as exc:
            return ToolResult(
                output=f"schedule_create failed: {exc}",
                is_error=True,
                caller_error=True,
            )
        state = "on" if cfg.enabled else "off until you turn it on"
        return ToolResult(
            output=f"job '{cfg.name}' created (cadence={cfg.cadence}, {state})",
            receipt=Receipt(
                kind="job",
                id=cfg.name,
                locator=str(scheduler.config_dir / "schedule.yaml"),
            ),
            metadata={
                "name": cfg.name,
                "cadence": cfg.cadence,
                "handler": cfg.handler,
                "enabled": cfg.enabled,
                "on_failure": cfg.on_failure,
            },
        )
