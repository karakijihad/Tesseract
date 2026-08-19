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

from tesseract.config.cost_caps import load_loop_cost_caps, require_cap
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.tools.brief_render import (
    _make_digester_invoker,
    _make_tavily_fetcher,
)
from tesseract.orchestrator.brief.pillars import DEFAULT_PILLARS
from tesseract.orchestrator.brief.renderer import BriefRenderer, CostCaps
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.brain.cost.metered_adapter import meter_chain
from tesseract.scheduler.role_chain import build_chain_for_role, resolve_role_name
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

    A named rule rather than an expression inside `run()` because it is the
    one thing about this stage that is easy to get quietly wrong.
    """
    return (anchor.astimezone(timezone.utc) + timedelta(days=1)).date()


class BriefRenderJob(BaseJob):
    uses_llm = True
    default_model_role = "agents_default"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            target_date = brief_date_for(ctx.fired_at)
            chain = meter_chain(_resolve_adapter_chain(ctx), ctx.cost_ledger)
            adapter, options = (chain[0] if chain else (None, AdapterOptions()))
            briefs_dir = _resolve_briefs_dir(ctx)
            interests_path = _resolve_interests_path(ctx)
            agents_dir = _resolve_agents_dir(ctx)
            memory_store = _resolve_memory_store(ctx)
            event_store = _resolve_event_store(ctx)
            caps = _resolve_cost_caps()

            vault_paths = _resolve_vault_paths(ctx)
            ecosystem_home = _resolve_ecosystem_home(ctx)
            renderer = BriefRenderer(
                briefs_dir=briefs_dir,
                pillars=DEFAULT_PILLARS,
                interests_path=interests_path,
                invoke_digester=_make_digester_invoker(adapter, options, agents_dir),
                tavily_search=_make_tavily_fetcher(None),  # no ToolContext in cron
                memory_store=memory_store,
                cost_caps=caps,
                event_store=event_store,
                vault_wiki_dir=vault_paths["wiki"],
                vault_raw_dir=vault_paths["raw"],
                librarian_compile=_resolve_librarian_compile(ctx),
                ecosystem_home=ecosystem_home,
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
                    "tavily_calls": result.tavily_calls,
                    "cost_cap_hit": result.cost_cap_hit,
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
    # MO-9-9 review fix: must anchor on TESSERACT_HOME (user-state root),
    # not ``app["tesseract_dir"]`` (source-package root). The Mirror Brief
    # tab reads from TESSERACT_HOME via the REST routes; a divergence
    # would land cron-written briefs under the source checkout where the
    # tab never looks. Resolve at call time so test monkeypatches reach.
    import os

    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    return home / "memory-store" / "daily" / "briefs"


def _resolve_interests_path(ctx: JobContext) -> Path:
    override = ctx.config.get("interests_path")
    if override:
        return Path(override)
    # Same TESSERACT_HOME late-binding pattern as _resolve_briefs_dir —
    # an operator's profile.yaml lives under their user-state root, not
    # the source tree.
    import os

    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    return home / "memory-store" / "interests" / "profile.yaml"


def _resolve_agents_dir(ctx: JobContext) -> Path | None:
    """`None` means both agent roots (AR-6) — the brief's digester cards are
    shipped, so they resolve out of the app tree unless the operator shadows
    one. A configured override restricts the load to that directory."""
    override = ctx.config.get("agents_dir")
    return Path(override) if override else None


def _resolve_vault_paths(ctx: JobContext) -> dict[str, Path]:
    """Both vault/wiki and vault/raw under TESSERACT_HOME. Wiki feeds the
    grounded vault-digest payload; raw receives auto-promoted world
    cards before the librarian compiles them to wiki pages.
    """
    import os

    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    wiki_override = ctx.config.get("vault_wiki_dir")
    raw_override = ctx.config.get("vault_raw_dir")
    return {
        "wiki": Path(wiki_override) if wiki_override else home / "vault" / "wiki",
        "raw": Path(raw_override) if raw_override else home / "vault" / "raw",
    }


def _resolve_ecosystem_home(ctx: JobContext) -> Path:
    """TESSERACT_HOME root the AU-24 ecosystem pre-fetcher walks for
    memory leaves, agenda items, docs-watch snapshots, and provider
    digests. Late-binds the env var like the brief/interests resolvers
    so test fixtures monkeypatching ``TESSERACT_HOME`` reach the data
    that fixture wrote into ``tmp_path``."""
    import os

    override = ctx.config.get("ecosystem_home")
    if override:
        return Path(override)
    return Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()


def _resolve_librarian_compile(ctx: JobContext):
    """Bound ``vault_librarian.compile_source`` from the Mirror app, or
    None when the scheduler ran without an app context (REPL bootstrap,
    test harness). Without it the renderer writes raw files but no
    auto-compile fires — the operator can still ingest manually."""
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return None
    librarian = app.get("vault_librarian")
    if librarian is None:
        return None
    compile_fn = getattr(librarian, "compile_source", None)
    if compile_fn is None:
        return None
    return compile_fn


def _resolve_memory_store(ctx: JobContext):
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return None
    bundle = app.get("memory_bundle")
    return getattr(bundle, "store", None) if bundle is not None else None


def _resolve_event_store(ctx: JobContext):
    """Workspace EventStore for the daily_brief newsletter card.

    Wired in MO-9-14 — the cron path emits a `daily_brief` workspace
    event so the operator sees yesterday's brief in the workspace
    stream every morning. Returns None when the Mirror app hasn't
    booted (REPL / cold scheduler invocation); the markdown write is
    still canonical.
    """
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return None
    return app.get("workspace_event_store")


def _resolve_cost_caps() -> CostCaps:
    """Read the ceilings from ``permissions.yaml::loop_cost_caps``.

    The job used to read its own mirrored copy out of ``schedule.yaml``, so
    editing the policy file — which every comment in the tree names as the
    authority — changed nothing. The mirror is gone; this is the one source.
    """
    caps = load_loop_cost_caps()
    return CostCaps(
        max_usd=require_cap(caps, "daily_brief_max_usd"),
        max_tavily_calls=int(require_cap(caps, "daily_brief_max_tavily_calls")),
    )


def _resolve_adapter_chain(ctx: JobContext) -> list[tuple[ModelAdapter, AdapterOptions]]:
    """Same precedence as ``provider_watch._resolve_adapter_chain``."""
    role_name = resolve_role_name(ctx, BriefRenderJob.default_model_role)
    app = ctx.app
    override_set = bool((ctx.model_role or "").strip())
    if override_set and role_name is not None:
        return build_chain_for_role(role_name, log_label="brief_render")
    if app is not None and hasattr(app, "get"):
        live = app.get("adapter_chain") or []
        if live:
            return [(a, o or AdapterOptions()) for a, o in live if a is not None]
    if role_name is not None:
        built = build_chain_for_role(role_name, log_label="brief_render")
        if built:
            return built
    if app is None or not hasattr(app, "get"):
        return []
    adapter = app.get("adapter")
    if adapter is None:
        return []
    return [(adapter, app.get("adapter_options") or AdapterOptions())]


__all__ = ["BriefRenderJob", "brief_date_for"]
