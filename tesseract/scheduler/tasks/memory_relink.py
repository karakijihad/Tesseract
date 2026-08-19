"""MemoryRelinkJob — re-link the memories the atlas says nothing can reach.

Triggered by the finding rather than by a clock: the stage declares
`reads=("atlas",)`, so it runs when the builder produced an atlas to read and
is skipped when the builder failed. No new schedule row exists for it, and
none should — the ordering is the declaration.

It needs embeddings, which is a provider, so it says so in its kind. With none
reachable it reports `degraded` with the reason rather than a clean night:
"nothing to link with" and "nothing needed linking" are opposite facts.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from tesseract.orchestrator.atlas import store as atlas_store
from tesseract.orchestrator.atlas.config import load_atlas_config
from tesseract.orchestrator.atlas.relink import relink_orphans
from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.tasks.atlas_build import _injected, _resolve_memory_store
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class MemoryRelinkJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            atlas = atlas_store.load()
            if not atlas.nodes:
                return _result(
                    ctx, t0,
                    detail="no atlas",
                    payload={"linked": 0, "attempted": 0},
                    outcome=RunOutcome.SKIPPED_NO_WORK,
                    reason="there is no atlas yet, so nothing has been found to repair",
                )
            store = _resolve_memory_store(ctx)
            linker = _resolve_linker(ctx, store)
            if linker is None:
                return _result(
                    ctx, t0,
                    detail="no linker",
                    payload={"linked": 0, "attempted": 0},
                    outcome=RunOutcome.DEGRADED,
                    reason=(
                        "the auto-linker needs the embedding index and this "
                        "process has no handle on it, so orphans stay orphans"
                    ),
                )

            cap = load_atlas_config().relink.max_per_run
            report = await relink_orphans(
                atlas, store=store, linker=linker, max_per_run=cap
            )
            payload = {
                "linked": report.linked,
                "attempted": report.attempted,
                "declined": dict(report.declined),
                "over_cap": report.skipped_over_cap,
            }
            detail = (
                f"linked={report.linked} attempted={report.attempted} "
                f"declined={sum(report.declined.values())}"
                + (f" over_cap={report.skipped_over_cap}" if report.skipped_over_cap else "")
            )
            if report.blocked_on_embeddings:
                return _result(
                    ctx, t0, detail=detail, payload=payload,
                    outcome=RunOutcome.DEGRADED,
                    reason=(
                        "embeddings were unreachable, which is the same thing "
                        "that orphaned these memories when they were written"
                    ),
                )
            if not report.attempted:
                return _result(
                    ctx, t0, detail=detail, payload=payload,
                    outcome=RunOutcome.SKIPPED_NO_WORK,
                    reason="the atlas found no memory that nothing points at",
                )
            if not report.linked:
                return _result(
                    ctx, t0, detail=detail, payload=payload,
                    outcome=RunOutcome.SKIPPED_NO_WORK,
                    reason=(
                        f"{report.attempted} orphan(s) still have no neighbour "
                        "close enough to link; they are retried as the store grows"
                    ),
                )
            return _result(ctx, t0, detail=detail, payload=payload)
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("memory_relink crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
                outcome=RunOutcome.FAILED,
                outcome_reason=f"the repair crashed: {type(exc).__name__}",
            )


def _result(
    ctx: JobContext,
    t0: float,
    *,
    detail: str,
    payload: dict,
    outcome: RunOutcome = RunOutcome.SUCCEEDED,
    reason: str = "",
) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=True,
        detail=detail,
        payload=payload,
        duration_ms=(time.monotonic() - t0) * 1000.0,
        outcome=outcome,
        outcome_reason=reason,
    )


def _resolve_linker(ctx: JobContext, store: Any) -> Any:
    """Injection, then the live bundle. No third fallback: an `AutoLinker`
    built here would need an embedding index this process has no way to make,
    and constructing a broken one is how a repair silently does nothing."""
    injected = _injected(ctx, "auto_linker")
    if injected is not None:
        return injected
    app = ctx.app
    bundle = app.get("memory_bundle") if app is not None and hasattr(app, "get") else None
    embeddings = getattr(bundle, "embeddings", None)
    if embeddings is None:
        return None
    from tesseract.memory.auto_linker import AutoLinker

    return AutoLinker(store=store, embeddings=embeddings)


__all__ = ["MemoryRelinkJob"]
