"""LibrarianHeartbeatJob — scheduled `Librarian.run_pass()`.

Nightly/Daily consolidation of the raw daily-capture layer into canonical
per-type subdirs, plus MEMORY.md refresh. Cadence lives in
`tesseract/config/schedule.yaml` (default 15:00 daily). Still invokable
manually via Mirror `/reflect`.

After consolidation, runs personality distillation: reads the last 7 days
of diary entries and the current SOUL.md `## Growth` section, asks the
adapter for stable observations, and writes 0-3 candidates to
`memory-store/pending_growth.md`. The librarian never edits SOUL.md
itself — `pending_growth.md` is a proposal surface that the assistant reviews at
session-end reflection.

Embedding-failure-tolerant: `Librarian` itself falls back to
source-path idempotency when Ollama is offline (see
`tesseract/memory/librarian.py` audit-2 remediation).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class LibrarianHeartbeatJob(BaseJob):
    uses_llm = True
    default_model_role = "chat_brain"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            bundle = _resolve_bundle(ctx)
            if bundle is None or getattr(bundle, "librarian", None) is None:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="memory_bundle unavailable",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            stats = await bundle.librarian.run_pass()
            promoted = stats.get("promoted", 0)
            deduped = stats.get("deduped", 0)
            skipped = stats.get("skipped", 0)

            distill_stats = await _run_distillation(ctx, bundle.librarian)
            payload = dict(stats)
            payload["distilled"] = distill_stats

            candidates = distill_stats.get("candidates", 0)
            # This used to publish an agenda candidate whenever distillation
            # produced something. It was the third publisher into one source,
            # and it was telling the assistant something it is already told:
            # the session-reflection prompt sends it to `pending_growth.md` by
            # name. The count stays in the payload, which the stage's manifest
            # row carries.
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=(
                    f"promoted={promoted} deduped={deduped} "
                    f"skipped={skipped} distilled={candidates}"
                ),
                payload=payload,
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("librarian_heartbeat crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_bundle(ctx: JobContext):
    app = ctx.app
    if app is None:
        return None
    if hasattr(app, "get"):
        return app.get("memory_bundle")
    return None


def _resolve_soul_path(ctx: JobContext) -> Path | None:
    """SOUL.md under the operator's workspace, resolved at call time via
    `tesseract.paths.workspace_dir()` so an app update replacing the code
    tree never touches it. `ctx` kept for call-site compatibility."""
    from tesseract.paths import workspace_dir
    return workspace_dir() / "SOUL.md"


async def _run_distillation(ctx: JobContext, librarian) -> dict:
    """Best-effort personality distillation. Never raises — returns a
    `{candidates: int, reason?: str}` dict so the heartbeat result is
    never blocked by distillation failures.

    Routes through `ctx.model_role` (operator override on this job in
    schedule.yaml) when set, falling back to `LibrarianHeartbeatJob`'s
    declared default role. Empty chain → distill returns `adapter_offline`
    after the librarian's null-chain branch fires.
    """
    soul_path = _resolve_soul_path(ctx)
    if soul_path is None:
        return {"candidates": 0, "reason": "soul_path_unresolved"}
    chain = build_chain_for_job(
        ctx,
        default_role=LibrarianHeartbeatJob.default_model_role,
        log_label="librarian_heartbeat",
    )
    try:
        return await librarian.distill_personality_candidates(
            soul_path, adapter_chain=chain or None,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort sidecar
        log.warning("distillation crashed: %r", exc)
        return {"candidates": 0, "reason": "exception"}
