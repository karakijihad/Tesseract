"""ConversationReflectJob — the capture funnel's third stage.

Every entry point is read, every conversation that has gone quiet earns the
same record, and the record says which door it came through. It replaces the
`channel_reflection_sweep` row, which did this for one channel and only while
the bridge that owned it was running.

Deterministic: no model is called here. What the day was ABOUT is the nightly
row's question; what was said is already on disk, and losing it because a
provider was unreachable is the failure this stage exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from tesseract.capture.reflect import ReflectOutcome, reflect
from tesseract.capture.sources import COLLECTORS, Conversation, idle_conversations
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.pipeline.artifacts import WatermarkStore
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_REQUIRED_KEYS = ("idle_minutes", "lookback_hours", "tail_turns", "max_per_tick")


class ConversationReflectJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        missing = [key for key in _REQUIRED_KEYS if key not in ctx.config]
        if missing:
            return _result(
                ctx, t0, ok=False, detail=f"missing config key(s): {', '.join(missing)}"
            )

        idle_minutes = int(ctx.config["idle_minutes"])
        lookback_hours = int(ctx.config["lookback_hours"])
        tail_turns = int(ctx.config["tail_turns"])
        max_per_tick = int(ctx.config["max_per_tick"])

        try:
            bundle = ctx.app.get("memory_bundle") if hasattr(ctx.app, "get") else None
            if bundle is None:
                return _result(ctx, t0, ok=True, detail="no memory bundle")

            # The collectors share nothing and both walk the disk, so they run
            # together; one unreadable tree must not cost the other's recaps.
            gathered = await asyncio.gather(
                *(
                    asyncio.to_thread(collect, tail_turns, lookback_hours=lookback_hours)
                    for collect in COLLECTORS
                ),
                return_exceptions=True,
            )
            conversations: list[Conversation] = []
            unreadable = 0
            for collector, result in zip(COLLECTORS, gathered):
                if isinstance(result, BaseException):
                    unreadable += 1
                    log.exception(
                        "conversation_reflect: %s could not be read",
                        collector.__name__,
                        exc_info=result,
                    )
                    continue
                conversations.extend(result)

            due = idle_conversations(
                conversations,
                now=datetime.now(timezone.utc),
                idle_minutes=idle_minutes,
                lookback_hours=lookback_hours,
            )
            # Oldest first, so a tick that hits the ceiling leaves the freshest
            # for the next one rather than starving what has waited longest.
            due.sort(key=lambda conv: conv.last_turn_at)
            held = max(0, len(due) - max_per_tick)
            due = due[:max_per_tick]

            watermarks = WatermarkStore()
            counts = {outcome: 0 for outcome in ReflectOutcome}
            failed = 0
            for conv in due:
                # Sequential: every one of these advances a position in the
                # single watermarks file, and two writers racing it lose one
                # another's positions — which costs a duplicate recap, the
                # exact defect this stage was built to stop.
                try:
                    outcome = await reflect(
                        conv,
                        bundle=bundle,
                        watermarks=watermarks,
                        now=datetime.now(timezone.utc),
                    )
                except Exception:
                    failed += 1
                    log.exception("conversation_reflect: %s could not be written", conv.key)
                    continue
                counts[outcome] += 1

            written = counts[ReflectOutcome.WRITTEN]
            amended = counts[ReflectOutcome.AMENDED]
            blocked = counts[ReflectOutcome.BLOCKED]
            return _result(
                ctx,
                t0,
                ok=True,
                detail=(
                    f"reflected={written} amended={amended} "
                    f"up_to_date={counts[ReflectOutcome.UP_TO_DATE]} "
                    f"blocked={blocked} failed={failed} held={held} "
                    f"conversations={len(conversations)}"
                ),
                payload={
                    "written": written,
                    "amended": amended,
                    "blocked": blocked,
                    "up_to_date": counts[ReflectOutcome.UP_TO_DATE],
                    "failed": failed,
                    "held": held,
                    "unreadable_sources": unreadable,
                    "conversations": len(conversations),
                    "due": len(due),
                },
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("conversation_reflect crashed")
            return _result(ctx, t0, ok=False, detail=f"unhandled: {exc!r}")


def _result(
    ctx: JobContext,
    t0: float,
    *,
    ok: bool,
    detail: str,
    payload: dict | None = None,
) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=ok,
        detail=detail,
        payload=payload or {},
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


__all__ = ["ConversationReflectJob"]
