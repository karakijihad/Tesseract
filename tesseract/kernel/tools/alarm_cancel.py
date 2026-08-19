"""alarm_cancel tool — remove an alarm entirely.

Removes a pending alarm from the YAML store. For recurring alarms this
deletes the whole rule; there is no 'skip next fire' state. Handle is
label (if unique) or 8-char id prefix. AUTO tier.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.scheduler.alarms import AlarmRegistry


class AlarmCancelInput(BaseModel):
    handle: str = Field(description="Alarm label (e.g. 'trash') or id prefix (e.g. '7a3f91b2'). Ambiguous labels error out.")


class AlarmCancelTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "time"
    summary: ClassVar[str] = "Cancels and deletes a pending alarm entirely, one-time or recurring."
    use_when: ClassVar[str] = (
        "Use when the operator wants an alarm gone for good. Call `alarm_list` first if unsure of the label."
    )
    not_when: ClassVar[str] = (
        "postponing an alarm's next fire, which is `alarm_snooze`; removing a scheduler job, "
        "which is `schedule_remove`."
    )

    def __init__(self, alarm_registry: AlarmRegistry) -> None:
        self._registry = alarm_registry

    @property
    def name(self) -> str:
        return "alarm_cancel"

    @property
    def input_schema(self) -> type[BaseModel]:
        return AlarmCancelInput

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: AlarmCancelInput = tool_input  # type: ignore[assignment]
        removed = self._registry.cancel(inp.handle)
        if removed is None:
            suggestions = self._registry.suggestions(inp.handle)
            msg = f"no alarm matches {inp.handle!r}"
            if suggestions:
                msg += f" — candidates: {', '.join(suggestions)}"
            return ToolResult(output=msg, is_error=True)
        return ToolResult(
            output=f"cancelled: {removed.label} [{removed.id[:8]}]",
            metadata={"id": removed.id, "label": removed.label},
        )
