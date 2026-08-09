"""AU-16 S1 — ``SealJob``.

For each ``LeafBuffer`` whose backlog crosses a size OR age threshold,
compresses the buffered leaves into a single ``Seal`` artefact, then
transitions every constituent leaf from ``BUFFERED`` to ``SEALED``.

The lexical S1 summariser lives in :mod:`tesseract.memory.leaf_seals`.
S2's tree modules consume the resulting seal files; S1 just produces
them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from tesseract.memory.leaf_buffers import LeafBuffer, buffers_root, iter_buffers
from tesseract.memory.leaf_seals import Seal, build_summary, mint_seal_id, write_seal
from tesseract.memory.leaves import LEAF_PIPELINE_LOCK, LeafState, LeafStore, MemoryLeaf
from tesseract.memory.trees.source_tree import write_seal_section
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


DEFAULT_MAX_BUFFER_LEAVES = 20
DEFAULT_MAX_BUFFER_AGE_SECONDS = 3600.0  # 1h


class SealJob(BaseJob):
    """Per-tick seal pass.

    Configuration via ``ctx.config``:

    - ``max_buffer_leaves``: count threshold per buffer (default 20).
    - ``max_buffer_age_seconds``: float seconds before a non-empty
      buffer seals even below the count threshold (default 3600).
    - ``max_seals_per_tick``: cap on seals produced per invocation
      (default 32) so a long backlog can't dominate one tick.
    - ``store_root`` / ``buffers_root``: optional path overrides.

    Empty buffers are skipped. Missing leaves (e.g. operator-deleted)
    are tracked in the result payload but do not abort the seal — the
    seal records only the leaves that resolved, and the buffer is
    cleared regardless to keep the source from re-firing on stale ids.
    """

    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        max_leaves = int(ctx.config.get("max_buffer_leaves", DEFAULT_MAX_BUFFER_LEAVES))
        max_age = float(
            ctx.config.get("max_buffer_age_seconds", DEFAULT_MAX_BUFFER_AGE_SECONDS)
        )
        max_seals = int(ctx.config.get("max_seals_per_tick", 32))
        store_root = ctx.config.get("store_root")
        buf_root_cfg = ctx.config.get("buffers_root")

        store = LeafStore(root=Path(store_root) if store_root else None)
        buf_root = Path(buf_root_cfg).resolve() if buf_root_cfg else buffers_root()
        now = datetime.now(timezone.utc)

        def _process() -> tuple[int, int, int, int]:
            seals_written = 0
            leaves_sealed = 0
            leaves_missing = 0
            buffers_walked = 0
            # Held for the whole pass — see `LEAF_PIPELINE_LOCK`'s
            # docstring. Must not interleave with `AppendBufferJob`
            # appending an id + transitioning its leaf to BUFFERED: this
            # loop's read-ids/lookup/clear for one buffer has to observe
            # that pair atomically or it can clear an id whose
            # transition hasn't landed, orphaning the leaf.
            with LEAF_PIPELINE_LOCK:
                for buffer in iter_buffers(root=buf_root):
                    buffers_walked += 1
                    if seals_written >= max_seals:
                        break

                    ids = buffer.read_ids()
                    if not ids:
                        continue

                    should_seal = (
                        len(ids) >= max_leaves
                        or buffer.stale(now=now, max_age_seconds=max_age)
                    )
                    if not should_seal:
                        continue

                    leaves: list[MemoryLeaf] = []
                    missing_here = 0
                    for leaf_id in ids:
                        leaf = store.get(leaf_id)
                        if leaf is None or leaf.state is not LeafState.BUFFERED:
                            missing_here += 1
                            continue
                        leaves.append(leaf)

                    leaves_missing += missing_here
                    if not leaves:
                        # Buffer pointed at gone leaves — clear it so we don't loop.
                        buffer.clear()
                        continue

                    title, body = build_summary(leaves, now=now)
                    seal = Seal(
                        seal_id=mint_seal_id(),
                        source_slug=buffer.source,
                        sealed_at=now,
                        leaf_ids=[lf.id for lf in leaves],
                        leaf_count=len(leaves),
                        summary_title=title,
                        summary_body=body,
                    )
                    write_seal(seal)
                    # AU-16 S2 — fold the seal into the per-source tree immediately.
                    # Topic + global trees catch up on their own cadences (those
                    # require cross-seal aggregation that doesn't belong in the
                    # sealing hot path).
                    try:
                        write_seal_section(seal)
                    except Exception:
                        log.exception(
                            "seal: source_tree write failed for %s — leaves still sealed",
                            seal.seal_id,
                        )

                    for lf in leaves:
                        lf.sealed_into = seal.seal_id
                        store.transition(lf, LeafState.SEALED, reason=seal.seal_id)
                        leaves_sealed += 1

                    buffer.clear()
                    seals_written += 1
            return buffers_walked, seals_written, leaves_sealed, leaves_missing

        buffers_walked, seals_written, leaves_sealed, leaves_missing = (
            await asyncio.to_thread(_process)
        )

        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail=(
                f"buffers={buffers_walked} seals={seals_written} "
                f"leaves_sealed={leaves_sealed} missing={leaves_missing}"
            ),
            payload={
                "buffers_walked": buffers_walked,
                "seals_written": seals_written,
                "leaves_sealed": leaves_sealed,
                "leaves_missing": leaves_missing,
            },
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )


__all__ = [
    "DEFAULT_MAX_BUFFER_AGE_SECONDS",
    "DEFAULT_MAX_BUFFER_LEAVES",
    "SealJob",
]
