"""Leaf intake — extraction and buffer append in one tick.

The two halves of this pipeline used to be separate jobs on identical
``*/5`` cadences. Extraction moves a leaf ``PENDING_EXTRACTION -> ADMITTED``
and append moves it ``ADMITTED -> BUFFERED``, so a leaf needed two ticks to
clear a pipeline that has no reason to pause in between. Running them in one
job halves the scheduler wake-ups and lets a leaf reach ``BUFFERED``
immediately.

The stages are composed rather than inlined: each keeps its own per-tick cap
and its own counters, so a failure stays attributable to the stage that
caused it instead of collapsing into one pass/fail.
"""

from __future__ import annotations

import dataclasses
import time

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.tasks.leaf_append import AppendBufferJob
from tesseract.scheduler.tasks.leaf_extract import ExtractChunkJob
from tesseract.scheduler.types import JobContext, JobResult

# The stages were tuned separately and should stay that way: extraction does
# real per-leaf work, appending is a cheap id push. Collapsing them onto one
# shared `max_per_tick` would silently retune whichever default lost.
_EXTRACT_DEFAULT_MAX = 64
_APPEND_DEFAULT_MAX = 256


def _stage_ctx(ctx: JobContext, max_per_tick: int) -> JobContext:
    """A per-stage context carrying that stage's cap.

    Both stages read ``max_per_tick`` from ``ctx.config``, so they each need
    their own view of it. Everything else — store roots, app handle, run id —
    is passed through untouched.
    """
    return dataclasses.replace(ctx, config={**ctx.config, "max_per_tick": max_per_tick})


class LeafIntakeJob(BaseJob):
    """Per-tick extraction + buffer append.

    Configuration via ``ctx.config``:

    - ``extract_max_per_tick``: leaves extracted per invocation (default 64).
    - ``append_max_per_tick``: leaves appended per invocation (default 256).
    - ``store_root``: optional ``LeafStore`` root override.
    - ``buffers_root``: optional buffers directory override.
    """

    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        extract_max = int(ctx.config.get("extract_max_per_tick", _EXTRACT_DEFAULT_MAX))
        append_max = int(ctx.config.get("append_max_per_tick", _APPEND_DEFAULT_MAX))

        extract = await ExtractChunkJob().run(_stage_ctx(ctx, extract_max))
        # Append runs unconditionally, not only when extraction admitted
        # something: leaves left ADMITTED by an earlier tick (or by a
        # different producer) must still be picked up.
        append = await AppendBufferJob().run(_stage_ctx(ctx, append_max))

        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=extract.ok and append.ok,
            detail=f"extract[{extract.detail}] append[{append.detail}]",
            payload={"extract": extract.payload, "append": append.payload},
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )


__all__ = ["LeafIntakeJob"]
