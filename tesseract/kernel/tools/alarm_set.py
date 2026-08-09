"""alarm_set tool — agent-controlled alarm queueing.

Same backing store as the operator's `/alarm-set` slash command
(`app["alarm_registry"]`). When the assistant sets an alarm mid-conversation, it
persists to YAML and fires through the Mirror toast just like the operator
did it. No file I/O, no network — AUTO tier (passthrough like `set_mood`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.scheduler.alarm_parser import ALARM_HANDLER_DOTPATH, parse_alarm_spec
from tesseract.scheduler.alarms import AlarmRegistry

logger = logging.getLogger(__name__)


class AlarmSetInput(BaseModel):
    label: str = Field(description="Short handle for the alarm (e.g. 'trash', 'standup'). Must be unique among pending alarms.")
    when: str = Field(description="When to fire. Examples: '20m', '1h30m', '9am', 'tomorrow at 9am', 'next mon at 14:00', 'in 2 hours'. May include recurrence prefix: 'daily', 'weekdays', 'every mon', 'every 2h'.")
    message: str = Field(default="", description="What to remind about. Shown in the Mirror toast.")


class AlarmSetTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    def __init__(self, alarm_registry: AlarmRegistry) -> None:
        self._registry = alarm_registry

    @property
    def name(self) -> str:
        return "alarm_set"

    @property
    def description(self) -> str:
        return (
            "Queue an alarm that fires as a Mirror toast. Use for time-bound "
            "reminders the operator asks for ('remind me in 20 minutes to ...', "
            "'every weekday at 9am tell me to stand up'). Returns the alarm id "
            "so you can cancel or snooze it later."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return AlarmSetInput

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: AlarmSetInput = tool_input  # type: ignore[assignment]
        now = datetime.now(timezone.utc)
        run_at, recurrence, parsed_message = parse_alarm_spec(inp.when, now)
        if run_at is None or run_at <= now:
            return ToolResult(
                output=f"cannot parse when-expression: {inp.when!r}",
                is_error=True,
            )
        message = inp.message.strip() or parsed_message
        try:
            alarm = self._registry.add(
                label=inp.label,
                run_at=run_at,
                handler_dotpath=ALARM_HANDLER_DOTPATH,
                message=message,
                recurrence=recurrence,
            )
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        summary = (
            f"alarm queued: {alarm.label} [{alarm.id[:8]}] at {alarm.run_at.isoformat()}"
            + (f" (recurring: {alarm.recurrence.kind})" if alarm.recurrence else "")
        )
        return ToolResult(
            output=summary,
            metadata={
                "id": alarm.id,
                "label": alarm.label,
                "run_at": alarm.run_at.isoformat(),
                "recurrence": alarm.recurrence.to_dict() if alarm.recurrence else None,
            },
        )
