"""AtlasBuildJob — derive the map, after the store has settled for the night.

Deterministic and cheap: it reads memory frontmatter and wiki frontmatter,
transcribes the relationships already written there, and writes one file under
`<TESSERACT_HOME>/atlas/`. No model, no network, no writes anywhere else.

Last in the nightly order on purpose. Running it before `memory_scrub` would
map links the scrub is about to repair, and running it before `index_rebuild`
would be harmless but pointless — the atlas describes the settled store or it
describes a moment nobody had.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from tesseract.orchestrator.atlas.build import run_build
from tesseract.orchestrator.outcome import RunOutcome
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class AtlasBuildJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            memory_store = _resolve_memory_store(ctx)
            vault_manager = _resolve_vault_manager(ctx)

            # Walks every memory and wiki page and hashes raw sources — file
            # IO measured in hundreds of milliseconds on this store and more
            # on a large one. The loop carries health and inbound turns.
            report = await asyncio.to_thread(
                run_build,
                memory_store=memory_store,
                vault_manager=vault_manager,
                now=ctx.fired_at,
            )
            detail = (
                f"nodes={report.nodes} edges={report.edges} "
                f"memories={report.memories} pages={report.pages}"
                + (" full-rederive" if report.full_rederive else "")
            )
            payload = {
                "nodes": report.nodes,
                "edges": report.edges,
                "memories": report.memories,
                "pages": report.pages,
                "hashes_reused": report.hashes_reused,
                "full_rederive": report.full_rederive,
                "atlas_path": report.path,
            }
            if not report.nodes:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail=detail,
                    payload=payload,
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                    outcome=RunOutcome.SKIPPED_NO_WORK,
                    outcome_reason="the memory store and the vault are both empty",
                )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=detail,
                payload=payload,
                duration_ms=(time.monotonic() - t0) * 1000.0,
                outcome=RunOutcome.SUCCEEDED,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("atlas_build crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
                outcome=RunOutcome.FAILED,
                outcome_reason=f"the build crashed: {type(exc).__name__}",
            )


def _home() -> Path:
    """Call-time, so a test pointing `TESSERACT_HOME` at `tmp_path` is read
    here rather than whatever the module saw at import."""
    override = os.environ.get("TESSERACT_HOME")
    return Path(override).resolve() if override else TESSERACT_HOME


def _injected(ctx: JobContext, key: str) -> Any | None:
    return ctx.config.get(key) if isinstance(ctx.config, dict) else None


def _resolve_memory_store(ctx: JobContext):
    """Injection, then the live bundle, then the store on disk.

    The last fallback is what keeps this stage runnable from
    `python -m tesseract.scheduler.pipeline --stage atlas_build`: the builder
    only reads files, so unlike `memory_lint` it needs no running backend, and
    an operator watching one stage run is the thing cron did well.
    """
    from tesseract.memory.store import MemoryStore

    injected = _injected(ctx, "memory_store")
    if injected is not None:
        return injected
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        store = getattr(app.get("memory_bundle"), "store", None)
        if store is not None:
            return store
    return MemoryStore(store_dir=_home() / "memory-store")


def _resolve_vault_manager(ctx: JobContext):
    from tesseract.memory.vault_manager import VaultManager

    injected = _injected(ctx, "vault_manager")
    if injected is not None:
        return injected
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        manager = app.get("vault_manager")
        if manager is not None:
            return manager
    return VaultManager(vault_root=_home() / "vault")


__all__ = ["AtlasBuildJob"]
