"""The capture row's schedule entry.

One row where there were two. The job holds no logic of its own: the stages are
declared in `pipeline/stages/capture.py` and the ordering, budgets and outcome
reading belong to the runner.
"""

from __future__ import annotations

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.pipeline.manifest import MemoryManifestStore
from tesseract.scheduler.pipeline.row_job import run_row
from tesseract.scheduler.pipeline.stages.capture import CAPTURE_ROW
from tesseract.scheduler.types import JobContext, JobResult


class CapturePipelineJob(BaseJob):
    """Extract, buffer and seal. Deterministic; never calls a model."""

    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        # In-memory manifest: a row that fires again in five minutes has
        # nothing to resume, and a file per tick would be 288 a day.
        return await run_row(CAPTURE_ROW, ctx, manifests=MemoryManifestStore())


__all__ = ["CapturePipelineJob"]
