"""alarm_snooze tool — reschedule a pending alarm's next fire.

For recurring alarms only the upcoming fire shifts; the cycle itself is
unchanged. Duration defaults to 10m when omitted. AUTO tier.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.scheduler.alarm_parser import parse_alarm_when
from tesseract.scheduler.alarms import AlarmRegistry


class AlarmSnoozeInput(BaseModel):
    handle: str = Field(description="Alarm label or id prefix.")
    duration: str = Field(default="10m", description="How long to postpone. Accepts '5m', '1h', 'in 30 minutes', or a clock like '9am'. Defaults to 10m.")


class AlarmSnoozeTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "time"
    summary: ClassVar[str] = "Postpones a pending alarm's next fire without canceling it."
    use_when: ClassVar[str] = (
        "Use after the operator says snooze it or not now. A recurring alarm's cycle is kept."
    )
    not_when: ClassVar[str] = "deleting the alarm entirely, which is `alarm_cancel`."

    def __init__(self, alarm_registry: AlarmRegistry) -> None:
        self._registry = alarm_registry

    @property
    def name(self) -> str:
        return "alarm_snooze"

    @property
    def input_schema(self) -> type[BaseModel]:
        return AlarmSnoozeInput

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: AlarmSnoozeInput = tool_input  # type: ignore[assignment]
        now = datetime.now(timezone.utc)
        # Time-only parse — recurrence strings don't belong on a snooze.
        run_at = parse_alarm_when(inp.duration, now)
        if run_at is None or run_at <= now:
            return ToolResult(
                output=f"cannot parse snooze duration: {inp.duration!r}",
                is_error=True,
            )
        alarm = self._registry.snooze(inp.handle, run_at)
        if alarm is None:
            suggestions = self._registry.suggestions(inp.handle)
            msg = f"no alarm matches {inp.handle!r}"
            if suggestions:
                msg += f" — candidates: {', '.join(suggestions)}"
            return ToolResult(output=msg, is_error=True)
        return ToolResult(
            output=f"snoozed {alarm.label} [{alarm.id[:8]}] to {alarm.run_at.isoformat()}",
            metadata={"id": alarm.id, "label": alarm.label, "run_at": alarm.run_at.isoformat()},
        )
