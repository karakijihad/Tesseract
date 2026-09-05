"""BriefRenderJob — the brief is written at the anchor, not at breakfast.

It was an 08:00 row, which meant it read files written before the night's
pipeline touched them: the brief describing a day was assembled before most of
that day had been settled into memory. As the LAST stage of `consolidate` it
sees what the night did.

**The date moves with it.** A brief dated D covers the 24 hours ending at D
midnight — `activity.collect_yesterday_activity` anchors that window on
`target_date`, not on the wall clock. Rendered at the 23:00 anchor on day D,
the brief the operator reads next morning is therefore dated **D+1**: same
label they have always seen on the morning they read it, and now covering the
whole of D including the pipeline run that precedes this stage by minutes.

Delivery is not here. `mirror/server/brief_delivery.py` sends it at the hour
the operator set, which is a preference in `mirror.yaml` rather than a clock in
`schedule.yaml` — moving it moves nothing else.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.tools.brief_render import _make_digester_invoker
from tesseract.orchestrator.brief.renderer import BriefRenderer
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.brain.cost.metered_adapter import meter_chain
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


def brief_date_for(anchor: datetime) -> date:
    """Which brief a run at `anchor` writes: the morning after.

    A brief dated D covers the 24 hours ENDING at D midnight — that window is
    anchored on the brief's own date in `activity.collect_yesterday_activity`,
    not on the wall clock. So a run at 23:00 on D that wrote D would describe
    D-1 and miss the pipeline pass it is the tail of; writing D+1 covers the
    whole of D, and keeps the label the operator has always read on the
    morning they read it.

    **The day is the operator's, so it is read in their time.** The row fires
    on a cron matched against local time, but the engine hands the job a UTC
    instant, and converting that back to UTC gave the wrong day either side of
    the date line. At UTC-05 a 23:00 local run is already 04:00 UTC the next
    day, so this returned D+2 — a brief labelled two local days after the run
    that wrote it. `.astimezone()` with no argument converts to the system's
    own zone, which is the same clock the cron was matched against.

    A named rule rather than an expression inside `run()` because it is the
    one thing about this stage that is easy to get quietly wrong.
    """
    return (anchor.astimezone() + timedelta(days=1)).date()


class BriefRenderJob(BaseJob):
    uses_llm = True
    # A CHAIN, not a role. `agents_default` and `subagents_default` are seats
    # that also serve `invoke_agent`, so sharing one meant this job's model and
    # its spend moved whenever an agent was re-pointed. Naming the chain
    # directly severs that: what it spends bills to the entry, whose ceiling is
    # on its manifest entry.
    default_model_chain = "chain_1"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            target_date = brief_date_for(ctx.fired_at)
            chain = meter_chain(_resolve_adapter_chain(ctx), ctx.cost_ledger)
            adapter, options = (chain[0] if chain else (None, AdapterOptions()))
            briefs_dir = _resolve_briefs_dir(ctx)
            agents_dir = _resolve_agents_dir(ctx)
            memory_store = _resolve_memory_store(ctx)
            event_store = _resolve_event_store(ctx)

            renderer = BriefRenderer(
                briefs_dir=briefs_dir,
                invoke_digester=_make_digester_invoker(adapter, options, agents_dir),
                memory_store=memory_store,
                event_store=event_store,
                vault_wiki_dir=_resolve_vault_wiki_dir(ctx),
                home=_resolve_home(ctx),
            )
            # `overwrite=False` — an operator who ran `/brief` for that date
            # already has the one they asked for, and re-rendering would both
            # re-bill the digesters and replace what they read.
            result = await renderer.render(target_date, overwrite=False)
            duration_ms = (time.monotonic() - t0) * 1000.0
            if result.skipped_existing:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail=f"brief for {target_date.isoformat()} already exists",
                    payload={
                        "target_date": target_date.isoformat(),
                        "path": str(result.path),
                        "skipped_existing": True,
                    },
                    duration_ms=duration_ms,
                )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=(
                    f"wrote brief for {target_date.isoformat()} "
                    f"(sections={len(result.sections_rendered)})"
                ),
                payload={
                    "target_date": target_date.isoformat(),
                    "path": str(result.path),
                    "sections_rendered": result.sections_rendered,
                    "memory_id": result.memory_id,
                    "workspace_event_id": result.workspace_event_id,
                },
                duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("brief_render crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_briefs_dir(ctx: JobContext) -> Path:
    override = ctx.config.get("briefs_dir")
    if override:
        return Path(override)
    # Must anchor on TESSERACT_HOME (user-state root),
    # not ``app["tesseract_dir"]`` (source-package root). The Mirror Brief
    # tab reads from TESSERACT_HOME via the REST routes; a divergence
    # would land cron-written briefs under the source checkout where the
    # tab never looks. Resolve at call time so test monkeypatches reach.
    import os

    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    return home / "memory-store" / "daily" / "briefs"


def _resolve_agents_dir(ctx: JobContext) -> Path | None:
    """`None` means both agent roots — the brief's digester cards are
    shipped, so they resolve out of the app tree unless the operator shadows
    one. A configured override restricts the load to that directory."""
    override = ctx.config.get("agents_dir")
    return Path(override) if override else None


def _resolve_vault_wiki_dir(ctx: JobContext) -> Path:
    """``vault/wiki`` under TESSERACT_HOME. It grounds the vault-digest
    payload with the ingest-log rows inside the window."""
    import os

    override = ctx.config.get("vault_wiki_dir")
    if override:
        return Path(override)
    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    return home / "vault" / "wiki"


def _resolve_home(ctx: JobContext) -> Path:
    """TESSERACT_HOME root the agenda store is read under. Late-binds the
    env var like the briefs-dir resolver so test fixtures monkeypatching
    ``TESSERACT_HOME`` reach the data that fixture wrote into
    ``tmp_path``."""
    import os

    override = ctx.config.get("home")
    if override:
        return Path(override)
    return Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()


def _resolve_memory_store(ctx: JobContext):
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return None
    bundle = app.get("memory_bundle")
    return getattr(bundle, "store", None) if bundle is not None else None


def _resolve_event_store(ctx: JobContext):
    """Workspace EventStore for the daily_brief newsletter card.

    The cron path emits a `daily_brief` workspace event so the operator
    sees yesterday's brief in the workspace
    stream every morning. Returns None when the Mirror app hasn't
    booted (REPL / cold scheduler invocation); the markdown write is
    still canonical.
    """
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return None
    return app.get("workspace_event_store")


def _resolve_adapter_chain(ctx: JobContext) -> list[tuple[ModelAdapter, AdapterOptions]]:
    """The chain this row rides, billed to the row.

    It used to prefer the app's LIVE adapter chain over its own, and that
    chain is chat_brain's: the brief was rendered on the conversational
    model and its spend was charged to the conversational cap. The
    optimisation the branch existed for is gone anyway, because a built chain
    is `LazyAdapter`s and costs a dict lookup until something generates.
    """
    return build_chain_for_job(
        ctx,
        default_role=None,
        default_chain=BriefRenderJob.default_model_chain,
        log_label="brief_render",
    )


__all__ = ["BriefRenderJob", "brief_date_for"]
