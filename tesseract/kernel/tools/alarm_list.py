"""alarm_list tool — TARS-readable view of pending alarms.

Returns id, label, run_at, message, recurrence for every pending alarm.
Pure read — AUTO tier.
"""

from __future__ import annotations

from pydantic import BaseModel

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.scheduler.alarms import AlarmRegistry


class AlarmListInput(BaseModel):
    pass


class AlarmListTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    def __init__(self, alarm_registry: AlarmRegistry) -> None:
        self._registry = alarm_registry

    @property
    def name(self) -> str:
        return "alarm_list"

    @property
    def description(self) -> str:
        return (
            "List every pending alarm. Use before canceling or snoozing so you "
            "know the labels/ids the operator can disambiguate with."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return AlarmListInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        alarms = self._registry.list_pending()
        if not alarms:
            return ToolResult(output="no pending alarms", metadata={"count": 0, "alarms": []})
        lines = []
        entries = []
        for a in alarms:
            rec = f" recurring={a.recurrence.kind}" if a.recurrence else ""
            lines.append(f"- {a.label} [{a.id[:8]}] at {a.run_at.isoformat()}{rec} — {a.message}")
            entries.append({
                "id": a.id,
                "label": a.label,
                "run_at": a.run_at.isoformat(),
                "message": a.message,
                "recurrence": a.recurrence.to_dict() if a.recurrence else None,
            })
        return ToolResult(
            output="\n".join(lines),
            metadata={"count": len(alarms), "alarms": entries},
        )
