"""AU-16 S1 — ``AppendBufferJob``.

Picks up every leaf in ``LeafState.ADMITTED`` and appends its id to the
matching ``LeafBuffer``. Transitions the leaf to ``BUFFERED``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from tesseract.memory.leaf_buffers import LeafBuffer, buffers_root
from tesseract.memory.leaves import LEAF_PIPELINE_LOCK, LeafState, LeafStore
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class AppendBufferJob(BaseJob):
    """Per-tick buffer fan-out.

    Configuration via ``ctx.config``:

    - ``max_per_tick``: cap on leaves processed per invocation (default 256).
    - ``store_root``: optional ``LeafStore`` root override.
    - ``buffers_root``: optional buffers directory override.
    """

    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        max_per_tick = int(ctx.config.get("max_per_tick", 256))
        store_root = ctx.config.get("store_root")
        buf_root_cfg = ctx.config.get("buffers_root")

        store = LeafStore(root=Path(store_root) if store_root else None)
        buf_root = Path(buf_root_cfg).resolve() if buf_root_cfg else buffers_root()

        def _process() -> tuple[int, int, int]:
            processed = 0
            appended = 0
            errors = 0
            # Held for the whole pass — see `LEAF_PIPELINE_LOCK`'s
            # docstring. Must not interleave with `SealJob` reading this
            # same buffer + clearing it: an id appended here has to be
            # visible together with its leaf's BUFFERED transition, or
            # a seal landing in between can clear the id before the
            # transition lands and orphan the leaf.
            with LEAF_PIPELINE_LOCK:
                for leaf in store.list_in_state(LeafState.ADMITTED):
                    if processed >= max_per_tick:
                        break
                    processed += 1
                    try:
                        buffer = LeafBuffer(leaf.source, root=buf_root)
                        buffer.append(leaf.id)
                        store.transition(leaf, LeafState.BUFFERED, reason="appended")
                        appended += 1
                    except Exception:
                        log.exception(
                            "append_buffer: leaf %s raised — leaving in admitted",
                            leaf.id,
                        )
                        errors += 1
            return processed, appended, errors

        processed, appended, errors = await asyncio.to_thread(_process)

        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail=f"processed={processed} appended={appended} errors={errors}",
            payload={
                "processed": processed,
                "appended": appended,
                "errors": errors,
            },
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )


__all__ = ["AppendBufferJob"]
