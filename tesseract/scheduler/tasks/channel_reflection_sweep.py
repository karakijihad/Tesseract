"""ChannelReflectionSweepJob — restart-resilient end-of-conversation memory.

Replaces the in-process ``asyncio.sleep(1800)`` reflection timer from
Session 1 with a scheduler-driven sweep. The old design was lost on
every Mirror restart because the deferred task was cancelled by
``bridge.stop()`` before it could fire; this job rides the same cron
rails as ``daily_brief`` / ``conscience_heartbeat`` so the reflection
fires regardless of process lifetime.

Per tick:
1. Walk every registered channel adapter.
2. For each chat with state, compute idle since ``last_message_ts``.
3. If idle ≥ ``reflection_delay_s`` and no reflection has been written
   since that last message, fire :meth:`ChatMemoryService._write_reflection`
   synchronously. The service's existing idempotency check (last
   reflected ts == newest tail ts → no-op) prevents double-fires when
   the sweep runs faster than turns land.

Default cadence: every 5 minutes. Daily reflections still fire within
``reflection_delay_s + 5 min`` of true idle.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_DEFAULT_REFLECTION_DELAY_S = 1800  # match ChatMemoryService default


class ChannelReflectionSweepJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        delay_s = int(ctx.config.get("reflection_delay_s", _DEFAULT_REFLECTION_DELAY_S))
        fired = 0
        scanned = 0
        skipped_idle = 0

        from tesseract.integrations import list_channels

        adapters = list_channels()
        if not adapters:
            return JobResult(
                job_name=ctx.job_name, run_id=ctx.run_id, ok=True,
                detail="no channel adapters registered",
                payload={"fired": 0, "scanned": 0},
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

        now = datetime.now(timezone.utc)
        for adapter in adapters:
            chat_memory = getattr(adapter, "_chat_memory", None)
            state_bundle = getattr(adapter, "_state", None)
            if chat_memory is None or state_bundle is None:
                continue
            poll_state = getattr(state_bundle, "poll_state", None)
            if poll_state is None:
                continue
            last_seen: dict[str, str] = getattr(poll_state, "last_message_ts", {}) or {}

            channel_name = getattr(adapter, "name", "")
            for chat_key, last_iso in list(last_seen.items()):
                scanned += 1
                if not isinstance(last_iso, str) or not last_iso:
                    continue
                try:
                    last_at = datetime.fromisoformat(last_iso)
                except ValueError:
                    continue
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=timezone.utc)
                idle_s = (now - last_at).total_seconds()
                if idle_s < delay_s:
                    skipped_idle += 1
                    continue
                # Fire the existing writer. The real double-write guard
                # is inside ``_write_reflection``: it compares the tail's
                # newest ts against ``state.last_reflected_turn_ts`` and
                # short-circuits when they match. That comparison reads
                # the conversation_store directly so it remains correct
                # even after a cold restart (the in-memory
                # ``_reflection_state`` dict is best-effort and resets
                # per process).
                try:
                    await chat_memory._write_reflection(channel_name, chat_key)
                    fired += 1
                except Exception:
                    log.exception(
                        "channel_reflection_sweep: writer crashed for %s/%s",
                        channel_name, chat_key,
                    )

        return JobResult(
            job_name=ctx.job_name, run_id=ctx.run_id, ok=True,
            detail=(
                f"reflection sweep: fired={fired} scanned={scanned} "
                f"skipped_idle={skipped_idle} delay_s={delay_s}"
            ),
            payload={"fired": fired, "scanned": scanned, "skipped_idle": skipped_idle},
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )
