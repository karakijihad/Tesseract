"""DreamCycleJob — nightly memory consolidation.

Drives `MemoryBundle.dreaming.run_cycle()`:

1. Build recall candidates from `recall.jsonl` (written by
   `RetrievalPipeline._log_recalls` on every `memory_search` hit).
2. Score each candidate (frequency / relevance / diversity / recency).
3. Promote winners into `MEMORY.md` so they survive across sessions.
4. Archive daily notes older than 30 days.
5. Sweep entries with a `source_path` but no wikilink and backfill them.
6. Trim promoted lines from the recall log.

Audit M2 fix (2026-04-29): before this, the engine class existed but
nothing scheduled `run_cycle()`, so the recall log grew without ever
being consumed and no memories ever made it from daily notes into
durable storage. The recall log path is wired here via
`MemoryBundle.recall_log_path` and matches what `RetrievalPipeline`
writes.
"""

from __future__ import annotations

import asyncio
import logging
import time

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


async def _broadcast_state(app, state: str) -> None:
    """Push an `entity_state_set` envelope to every active Mirror session.

    Mirrors `log_forwarder._broadcast`'s shape — best-effort, swallow
    per-session errors so one dead WS can't block the rest. No-op when
    no Mirror is attached (CLI-only scheduler runs)."""
    if app is None or not hasattr(app, "get"):
        return
    sessions = list(app.get("server_sessions", {}).values())
    if not sessions:
        return
    try:
        from tesseract.mirror.server.envelope import make_entity_state_set
        from tesseract.mirror.server.session import send_envelope
    except ImportError:
        return
    for session in sessions:
        try:
            await send_envelope(
                session,
                make_entity_state_set(session.session_id, state=state),
            )
        except Exception:  # noqa: BLE001 — broadcast must not crash the job
            log.exception("dream_cycle: entity_state_set broadcast failed")


class DreamCycleJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        affect = _resolve_affect(ctx)
        prior_state = affect.state if affect is not None else None
        try:
            bundle = _resolve_bundle(ctx)
            if bundle is None:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="memory_bundle unavailable",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            engine = getattr(bundle, "dreaming", None)
            if engine is None:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="memory_bundle.dreaming is None",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            # Flip the orb to `dreaming` for the duration of the cycle so
            # operators watching the Mirror can see why the assistant just went
            # quiet. Restored in the finally block — never leave the orb
            # latched in dreaming if the job crashes.
            if affect is not None:
                affect.set("dreaming")
                await _broadcast_state(ctx.app, "dreaming")

            # Off the loop: `run_cycle` parses the recall log, scores every
            # candidate, reads each memory file, rewrites MEMORY.md and sweeps
            # the store — all synchronous, and its cost grows with the corpus.
            promoted = await asyncio.to_thread(engine.run_cycle)
            # Operator-visible nudge in the Workspace inbox when the cycle
            # actually changed something. Silent on no-op nights so the
            # inbox stays signal-only. Best-effort; failure to write the
            # nudge must not change the job's ok=True status.
            if promoted:
                try:
                    await _emit_dream_nudge(ctx.app, promoted)
                except Exception:
                    log.exception("dream_cycle: workspace nudge write failed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"promoted={len(promoted)}",
                payload={
                    "promoted": list(promoted),
                    "promoted_count": len(promoted),
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("dream_cycle crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        finally:
            if affect is not None and prior_state is not None:
                affect.set(prior_state)
                await _broadcast_state(ctx.app, prior_state)


async def _emit_dream_nudge(app, promoted) -> None:
    """Append a `nudge` workspace event listing what dream_cycle promoted.

    Skipped silently when no app handle (CLI-only scheduler runs) — there's
    no operator surface to notify. Covers the morning-after question
    "what did the assistant do overnight?" without noisy nightly toasts."""
    if app is None or not hasattr(app, "get"):
        return
    from tesseract.workspace_events.events import WorkspaceEvent
    from tesseract.workspace_events.broadcast import broadcast_workspace_event

    store = app.get("workspace_event_store")
    if store is None:
        return
    promoted_list = list(promoted)
    head = ", ".join(str(p) for p in promoted_list[:5])
    overflow = len(promoted_list) - 5
    summary = head + (f" (+{overflow} more)" if overflow > 0 else "")
    event = WorkspaceEvent.new(
        kind="nudge",
        source="orchestrator",
        title=f"Dream cycle promoted {len(promoted_list)} memor{'y' if len(promoted_list) == 1 else 'ies'}",
        summary=f"Overnight consolidation lifted: {summary}",
        payload={"promoted": promoted_list, "promoted_count": len(promoted_list)},
    )
    store.append_event(event)
    await broadcast_workspace_event(app, event)


def _resolve_bundle(ctx: JobContext):
    app = ctx.app
    if app is None:
        return None
    if hasattr(app, "get"):
        return app.get("memory_bundle")
    return None


def _resolve_affect(ctx: JobContext):
    """Pull the in-process EntityAffect holder off the Mirror app, when
    the cycle runs in-Mirror. None when the job runs from a CLI scheduler
    with no orb to drive."""
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return None
    return app.get("entity_affect")
