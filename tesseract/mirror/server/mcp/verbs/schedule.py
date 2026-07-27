"""schedule.* MCP verbs (P3) — route to the schedule_* kernel tools, which
drive the live SchedulerEngine via ``scheduler_provider``."""

from __future__ import annotations

from tesseract.kernel.tools.schedule_create import ScheduleCreateInput
from tesseract.kernel.tools.schedule_remove import ScheduleRemoveInput
from tesseract.kernel.tools.schedule_run import ScheduleRunInput
from tesseract.kernel.tools.schedule_update import ScheduleUpdateInput
from tesseract.mirror.server.mcp.verbs._base import make_tool_verb

schedule_create = make_tool_verb("schedule_create", ScheduleCreateInput)
schedule_update = make_tool_verb("schedule_update", ScheduleUpdateInput)
schedule_run = make_tool_verb("schedule_run", ScheduleRunInput)
schedule_remove = make_tool_verb("schedule_remove", ScheduleRemoveInput)

__all__ = ["schedule_create", "schedule_update", "schedule_run", "schedule_remove"]
