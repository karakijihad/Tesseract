"""The consolidate row's schedule entry — the one clock time in the system.

Its cron IS the anchor: the stages have order, not clocks, so moving the row
moves all sixteen together. The job holds no logic of its own; the declaration
lives in `pipeline/stages/consolidate.py`.
"""

from __future__ import annotations

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.pipeline.row_job import run_row
from tesseract.scheduler.pipeline.stages.consolidate import CONSOLIDATE_ROW
from tesseract.scheduler.types import JobContext, JobResult


class ConsolidatePipelineJob(BaseJob):
    """The nightly pass that settles the day into memory."""

    # Four of the sixteen stages call a model; the row itself does not, and the
    # Schedule view's role dropdown belongs to those stages' own config blocks.
    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        # A disk manifest, unlike the capture row: this one runs once a night,
        # and a machine that dies mid-pass must resume at the stage it reached
        # rather than redo an evening of model calls.
        return await run_row(CONSOLIDATE_ROW, ctx)


__all__ = ["ConsolidatePipelineJob"]
