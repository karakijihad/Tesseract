"""schedule.* MCP verbs (P3) — route to the schedule_* kernel tools, which
drive the live SchedulerEngine via ``scheduler_provider``."""

from __future__ import annotations

from tesseract.kernel.tools.schedule_create import ScheduleCreateInput
from tesseract.kernel.tools.schedule_list import ScheduleListInput
from tesseract.kernel.tools.schedule_remove import ScheduleRemoveInput
from tesseract.kernel.tools.schedule_run import ScheduleRunInput
from tesseract.kernel.tools.schedule_update import ScheduleUpdateInput
from tesseract.mirror.server.mcp.verbs._base import (
    MCPVerbError,
    VerbContext,
    make_tool_verb,
    run_kernel_tool_result,
)

schedule_create = make_tool_verb("schedule_create", ScheduleCreateInput)
schedule_update = make_tool_verb("schedule_update", ScheduleUpdateInput)
schedule_run = make_tool_verb("schedule_run", ScheduleRunInput)
schedule_remove = make_tool_verb("schedule_remove", ScheduleRemoveInput)


async def schedule_list(ctx: VerbContext) -> list[dict]:
    """The job names every other verb here takes as an argument. Without it a
    client could create, update, run and remove rows it had no way to
    enumerate — names were knowable only to whoever created them in that
    session.

    Hand-written rather than ``make_tool_verb`` because ``schedule_list``
    answers in ``metadata["jobs"]`` and says only "N job(s) registered" in
    ``output``; wrapping it would have shipped a verb that returns the count
    and withholds the list."""
    try:
        tool_input = ScheduleListInput.model_validate(ctx.params)
    except (TypeError, ValueError) as exc:
        raise MCPVerbError(400, f"schedule_list invalid params: {exc}")
    result = await run_kernel_tool_result(ctx, "schedule_list", tool_input, ask_fn=ctx.ask_fn)
    return list((result.metadata or {}).get("jobs") or [])


schedule_list.mcp_input_model = ScheduleListInput  # type: ignore[attr-defined]

__all__ = [
    "schedule_create",
    "schedule_update",
    "schedule_run",
    "schedule_remove",
    "schedule_list",
]
