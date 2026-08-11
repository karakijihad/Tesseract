"""SystemDiagnoseTool — ask the runtime about its own machine, mid-session.

The operator's framing: "perhaps one of the schedulers, or the agent himself
can run diagnosis on the logs? this will check if all systems are working,
gpu, cudas, models, runtime."

Read-only and free to call. It reports; it never remediates — deciding what to
do about an open breaker or a missing model is the operator's, and a tool that
quietly fixed things would make the report untrustworthy as evidence.

The checks live in `orchestrator/diagnostics.py` rather than here, because the
same question is asked at launch by the capability reconciler. One
implementation, two entry points.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class SystemDiagnoseInput(BaseModel):
    include_evidence: bool = Field(
        default=False,
        description=(
            "Include the raw evidence dict for each check (model lists, probe "
            "rows, per-job last runs). Verbose — leave off unless the summary "
            "line is not enough to answer the question."
        ),
    )


class SystemDiagnoseTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "system_diagnose"

    @property
    def description(self) -> str:
        return (
            "Report this machine's runtime health in one call: GPU and CUDA, "
            "whether Whisper and Kokoro can use the GPU, Ollama reachability "
            "and which models are really installed, last recorded provider "
            "probe per role, open circuit breakers, last scheduler run per "
            "job, and free disk. Read-only, local and localhost only, no "
            "billable calls."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return SystemDiagnoseInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        inp = (
            tool_input
            if isinstance(tool_input, SystemDiagnoseInput)
            else SystemDiagnoseInput(**tool_input.model_dump())
        )

        from tesseract.orchestrator.diagnostics import collect_diagnosis, render_text

        diagnosis = await collect_diagnosis()
        if context.cancel_event.is_set():
            raise asyncio.CancelledError

        output = render_text(diagnosis)
        metadata = {
            "worst": diagnosis.worst,
            "counts": {
                status: len(diagnosis.by_status(status))  # type: ignore[arg-type]
                for status in ("bad", "warn", "unknown", "ok")
            },
        }
        if inp.include_evidence:
            metadata["checks"] = [
                {
                    "name": check.name,
                    "status": check.status,
                    "detail": check.detail,
                    "evidence": check.evidence,
                }
                for check in diagnosis.checks
            ]

        # `bad` is a real finding about the machine, not a tool failure. Marking
        # it `is_error` would make a working tool look broken and would put a
        # correct answer on the error path.
        return ToolResult(output=output, metadata=metadata)
