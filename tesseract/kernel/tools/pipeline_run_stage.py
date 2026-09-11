"""pipeline_run_stage — run one step of the scheduled maintenance work now.

The nightly work is a row of ordered steps: settle the day into memory, repair
what is broken in it, rebuild the indexes, redraw the map of how everything
connects. Running one of them off-schedule was the thing cron did well, and
until now it survived only as `python -m tesseract.scheduler.pipeline --stage`:
runnable from a terminal on the machine, and from nowhere else. The Autonomy
panel's Atlas room is the first surface to want one, and the answer to that
cannot be a second way of running a step.

So this is a tool rather than a Mirror command, and the difference is what the
operator can reach. A Mirror slash command is served by the cockpit's own
dispatcher; a channel has a separate, read-only router of its own, so a command
added there would be a control that exists at the desk and nowhere else. A tool
is one implementation that the model can call on any surface, that the operator
can type as `/pipeline_run_stage` in the Mirror, and that a panel button
reaches through the same path every other gated act on that panel uses.

**What it will not do is a decision the runtime already holds.** Which steps
exist is the pipeline registry's, and whether one can run here is
`Stage.needs_app` against the running app. Both answers come back from
`run_stage`, in the same sentence wherever it was asked.

`default_posture="ask"`: these steps write to the memory store, the indexes and
the map. Not destructive, and every one of them runs unattended every night,
but a step fired by hand is work the operator did not schedule and it can spend
a provider call.

**The posture is for the MODEL asking, which is the whole point.** A slash
command the operator typed, and the panel button that sends one, run through
`run_slash`, which deliberately skips posture: the operator initiated it, so
there is nothing to confirm. The model calling this on any surface is asked.
That is the same division `workspace_decide` makes and it is why one tool can
serve both the button and the channel without either being wrong.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt

logger = logging.getLogger(__name__)


class PipelineRunStageInput(BaseModel):
    stage: str = Field(
        description=(
            "Which step to run, by the name the schedule gives it (for "
            "example 'atlas_build' to redraw the map of how things connect, "
            "or 'index_rebuild' to rebuild the search indexes)."
        ),
    )


class PipelineRunStageTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = (
        "Runs one step of the scheduled maintenance work now, off its schedule."
    )
    use_when: ClassVar[str] = (
        "Use when the operator wants one piece of the nightly upkeep done now "
        "rather than tonight: the map redrawn after they saved a lot, the "
        "search indexes rebuilt after a search came back thin, the memory "
        "store checked or repaired. It answers with what the step did, in the "
        "step's own words, so relay that rather than saying it was started."
    )
    not_when: ClassVar[str] = (
        "to run the whole night's work, which is the `consolidate` row and is "
        "started from the Managed system room; to find out how the last one "
        "went, which is `autonomy_read`; to clear an open breaker so a "
        "capability is tried again, which is `breaker_reset`."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "job"
    recovery_behaviour: ClassVar[str] = "queryable"

    def __init__(self, app_provider: Optional[Callable[[], Any]] = None) -> None:
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "pipeline_run_stage"

    @property
    def input_schema(self) -> type[BaseModel]:
        return PipelineRunStageInput

    def is_concurrency_safe(self) -> bool:
        # One step at a time. Two runs of the same step over the same store is
        # the shape the pipeline's own ordering exists to prevent.
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        del context
        from tesseract.scheduler.pipeline.run_stage import run_stage

        inp: PipelineRunStageInput = tool_input  # type: ignore[assignment]
        app = self._app_provider() if self._app_provider is not None else None
        result = await run_stage(inp.stage.strip(), app=app)
        if result.ran:
            # The room that shows this step's output is holding what it read
            # before the step ran. Without this the Atlas room's own comment
            # ("the map is re-read here when it lands") was not true of
            # anything: its only re-read was a 90 second timer, so a redraw
            # that took two seconds sat looking unfinished for a minute and a
            # half. Which room hears is `panel_refresh.STAGE_KEYS`, not
            # something this tool decides.
            from tesseract.orchestrator.panel_refresh import publish_stale_for_stage

            publish_stale_for_stage(result.stage)
        return ToolResult(
            output=result.line,
            is_error=not result.ran,
            receipt=(
                Receipt(kind="job", id=result.stage)
                if result.ran
                else Receipt.nothing()
            ),
            metadata={
                "stage": result.stage,
                "ran": result.ran,
                "outcome": result.outcome,
            },
        )


__all__ = ["PipelineRunStageInput", "PipelineRunStageTool"]
