"""DailyBriefJob — daily 08:00 morning-brief generator.

Same renderer the ``/brief`` slash uses; cron path passes
``overwrite=False`` so an operator manual ``/brief`` from earlier in
the day wins. When today's file already exists, this job exits with
``skipped_existing`` and a ``missed_slots`` payload entry per
``_shared/brief-renderer-spec.md`` idempotency contract.

Disabled by default in ``schedule.yaml``. Operator flips on after the
Mirror Brief tab lands (MO-9-9) so the first scheduled run is
visually verifiable.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.tools.brief_render import (
    _make_digester_invoker,
    _make_tavily_fetcher,
)
from tesseract.orchestrator.brief.pillars import DEFAULT_PILLARS
from tesseract.orchestrator.brief.renderer import BriefRenderer, CostCaps
from tesseract.paths import TESSERACT_HOME, agents_dir
from tesseract.scheduler.base_job import BaseJob
from tesseract.brain.cost.metered_adapter import meter_chain
from tesseract.scheduler.role_chain import build_chain_for_role, resolve_role_name
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class DailyBriefJob(BaseJob):
    uses_llm = True
    default_model_role = "agents_default"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            target_date = ctx.fired_at.astimezone(timezone.utc).date()
            chain = meter_chain(_resolve_adapter_chain(ctx), ctx.cost_ledger)
            adapter, options = (chain[0] if chain else (None, AdapterOptions()))
            briefs_dir = _resolve_briefs_dir(ctx)
            interests_path = _resolve_interests_path(ctx)
            agents_dir = _resolve_agents_dir(ctx)
            memory_store = _resolve_memory_store(ctx)
            event_store = _resolve_event_store(ctx)
            caps = _resolve_cost_caps(ctx)

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
            result = await renderer.render(target_date, overwrite=False)
            duration_ms = (time.monotonic() - t0) * 1000.0
            if result.skipped_existing:
                await _broadcast_brief_ready(
                    ctx.app,
                    date=target_date.isoformat(),
                    path=str(result.path),
                    summary=(
                        (result.body or "").strip().splitlines()[0]
                        if result.body
                        else ""
                    ),
                )
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail=f"missed_slots: brief for {target_date.isoformat()} already exists",
                    payload={
                        "target_date": target_date.isoformat(),
                        "path": str(result.path),
                        "skipped_existing": True,
                    },
                    duration_ms=duration_ms,
                )
            await _broadcast_brief_ready(
                ctx.app,
                date=target_date.isoformat(),
                path=str(result.path),
                summary=(result.body or "").strip().splitlines()[0]
                if result.body
                else "",
            )
            await _broadcast_workspace_event_for_brief(
                ctx.app, event_store=event_store, event_id=result.workspace_event_id,
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
                },
                duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("daily_brief crashed")
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


def _resolve_agents_dir(ctx: JobContext) -> Path:
    override = ctx.config.get("agents_dir")
    if override:
        return Path(override)
    return agents_dir()


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


def _resolve_cost_caps(ctx: JobContext) -> CostCaps:
    block = ctx.config.get("cost_caps") or {}
    if not isinstance(block, dict):
        block = {}
    return CostCaps(
        max_usd=float(block.get("daily_brief_max_usd", CostCaps().max_usd)),
        max_tavily_calls=int(
            block.get("daily_brief_max_tavily_calls", CostCaps().max_tavily_calls),
        ),
    )


def _resolve_adapter_chain(ctx: JobContext) -> list[tuple[ModelAdapter, AdapterOptions]]:
    """Same precedence as ``provider_watch._resolve_adapter_chain``."""
    role_name = resolve_role_name(ctx, DailyBriefJob.default_model_role)
    app = ctx.app
    override_set = bool((ctx.model_role or "").strip())
    if override_set and role_name is not None:
        return build_chain_for_role(role_name, log_label="daily_brief")
    if app is not None and hasattr(app, "get"):
        live = app.get("adapter_chain") or []
        if live:
            return [(a, o or AdapterOptions()) for a, o in live if a is not None]
    if role_name is not None:
        built = build_chain_for_role(role_name, log_label="daily_brief")
        if built:
            return built
    if app is None or not hasattr(app, "get"):
        return []
    adapter = app.get("adapter")
    if adapter is None:
        return []
    return [(adapter, app.get("adapter_options") or AdapterOptions())]


async def _broadcast_workspace_event_for_brief(
    app: Any,
    *,
    event_store: Any,
    event_id: str | None,
) -> None:
    """Fan the new daily_brief workspace event out to open Mirror sessions.

    The renderer appended the event to disk; the workspace store's
    fan-out happens at the call site so the inbox refreshes without a
    manual reload. Fail-soft — a missed broadcast doesn't roll back the
    disk write.
    """
    if app is None or event_store is None or not event_id:
        return
    try:
        from tesseract.workspace_events.broadcast import broadcast_workspace_event
    except Exception:  # noqa: BLE001
        log.exception("daily_brief broadcast: workspace_events import failed")
        return
    try:
        event = event_store.get_event(event_id)
    except Exception:  # noqa: BLE001
        log.exception("daily_brief broadcast: get_event failed")
        return
    if event is None:
        return
    try:
        await broadcast_workspace_event(app, event)
    except Exception:  # noqa: BLE001
        log.exception("daily_brief broadcast: send failed")


async def _broadcast_brief_ready(
    app: Any,
    *,
    date: str,
    path: str,
    summary: str,
) -> None:
    """Fan a ``daily_brief_ready`` envelope from the cron path.

    Imported lazily so this module stays import-safe in REPL / standalone
    contexts where the Mirror is not loaded. Same fail-soft contract as
    ``broadcast_workspace_event``: never raise — a render that succeeded
    on disk must not be reported as failed because a WS pipe went away.
    """
    if app is None or not hasattr(app, "get"):
        return
    sessions = app.get("server_sessions") or {}
    if not sessions:
        await _push_brief_to_telegram(app, date=date)
        return
    try:
        from tesseract.mirror.server.routes.brief import broadcast_daily_brief_ready
    except Exception:
        log.exception("daily_brief broadcast: brief route import failed")
        await _push_brief_to_telegram(app, date=date)
        return
    try:
        await broadcast_daily_brief_ready(app, date=date, path=path, summary=summary)
    except Exception:
        log.exception("daily_brief broadcast: send failed")
        await _push_brief_to_telegram(app, date=date)
        return
    if app.get("brief_push_subscriber") is None:
        await _push_brief_to_telegram(app, date=date)


async def _push_brief_to_telegram(app: Any, *, date: str | None = None) -> None:
    """Fire the Telegram brief-push subscriber without requiring Mirror WS.

    ``broadcast_daily_brief_ready`` owns the normal Mirror-session path and
    already invokes this subscriber after it sends WS envelopes. With no
    open sessions it returns early, so the scheduler calls the subscriber
    directly here. Fail-soft: Telegram delivery must not invalidate the
    canonical brief written to disk.
    """
    if not hasattr(app, "get"):
        return
    push = app.get("brief_push_subscriber")
    if push is None:
        push = _build_brief_push_subscriber(app)
        if push is None:
            await _push_brief_via_telegram_api(app, date=date)
            return
    handle = getattr(push, "handle", None)
    if handle is None:
        await _push_brief_via_telegram_api(app, date=date)
        return
    try:
        result = await handle()
        log.info("daily_brief broadcast: brief_push result=%s", result)
        if _should_try_telegram_api_fallback(result):
            await _push_brief_via_telegram_api(app, date=date)
    except Exception:
        log.exception("daily_brief broadcast: brief_push subscriber failed")
        await _push_brief_via_telegram_api(app, date=date)


def _build_brief_push_subscriber(app: Any) -> Any:
    """Construct a ``TelegramBriefPushSubscriber`` on demand.

    The Mirror app normally wires ``brief_push_subscriber`` at bridge
    startup (``_wire_brief_push_subscriber``). In a scheduler-run app
    context that wiring may be absent, leaving ``_push_brief_to_telegram``
    a no-op. Rebuild it here from the same dependencies the Mirror uses,
    reading config/allowlist/tier at call time. Fail-soft: any missing
    dependency returns None so the disk write stays canonical.
    """
    try:
        from tesseract.integrations.telegram.brief_push import (
            TelegramBriefPushSubscriber,
        )
        from tesseract.integrations.telegram.state import load_allowlist
    except Exception:
        log.exception("daily_brief broadcast: brief_push import failed")
        return None

    telegram_bridge = app.get("telegram_bridge")
    event_store = app.get("workspace_event_store")
    if telegram_bridge is None or getattr(telegram_bridge, "_state", None) is None:
        return None
    if event_store is None:
        return None

    return TelegramBriefPushSubscriber(
        bridge=telegram_bridge,
        event_store=event_store,
        config_loader=lambda: app.get("channels_config"),
        allowlist_loader=lambda: load_allowlist(
            telegram_bridge._state.allowlist_path
        ),
        user_tier_loader=lambda: dict(telegram_bridge._state.poll_state.user_tier),
    )


def _should_try_telegram_api_fallback(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    reason = result.get("reason")
    if reason in {"disabled", "no_payload", "empty_text"}:
        return False
    sent = int(result.get("sent") or 0)
    skipped = int(result.get("skipped") or 0)
    errors = int(result.get("errors") or 0)
    if sent > 0 or skipped > 0:
        return False
    return reason == "no_bridge" or errors > 0


async def _push_brief_via_telegram_api(
    app: Any,
    *,
    date: str | None = None,
) -> dict[str, Any]:
    """Last-resort daily-brief push when the live bridge is unavailable.

    This preserves the operator-facing delivery path for scheduler runs
    where ``TELEGRAM_BOT_TOKEN`` and the allowlist exist but the Mirror
    bridge/subscriber was not wired. It intentionally does not raise:
    the markdown brief and workspace event remain the canonical result.
    """
    if not hasattr(app, "get"):
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_app"}
    if not _telegram_brief_push_enabled(app):
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "disabled"}

    try:
        from tesseract.integrations.telegram.brief_push import (
            format_exec_summary,
            send_to_operators,
        )
        from tesseract.integrations.telegram.api import TelegramAPI
        from tesseract.integrations.telegram.state import load_allowlist, load_state
    except Exception:
        log.exception("daily_brief broadcast: TelegramAPI fallback import failed")
        return {"sent": 0, "skipped": 0, "errors": 1, "reason": "import_failed"}

    payload = _latest_brief_payload(app, date=date)
    if payload is None:
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_payload"}
    text = format_exec_summary(payload)
    if not text:
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "empty_text"}

    import os

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return {"sent": 0, "skipped": 0, "errors": 0, "reason": "no_token"}

    state_dir = _resolve_telegram_state_dir()
    allowlist = load_allowlist(
        state_dir / "allowlist.json",
        env_seed=os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"),
    )
    tiers = load_state(state_dir / "state.json").user_tier
    try:
        api = TelegramAPI(token)
    except Exception:
        log.exception("daily_brief broadcast: TelegramAPI fallback init failed")
        return {"sent": 0, "skipped": 0, "errors": 1, "reason": "init_failed"}
    try:
        result = await send_to_operators(
            text,
            bridge=_TelegramAPISender(api),
            allowlist=allowlist,
            user_tier=dict(tiers),
        )
        log.info("daily_brief broadcast: TelegramAPI fallback result=%s", result)
        return result
    except Exception:
        log.exception("daily_brief broadcast: TelegramAPI fallback failed")
        return {"sent": 0, "skipped": 0, "errors": 1, "reason": "send_failed"}
    finally:
        try:
            await api.aclose()
        except Exception:
            log.exception("daily_brief broadcast: TelegramAPI fallback close failed")


def _telegram_brief_push_enabled(app: Any) -> bool:
    cfg = app.get("channels_config") if hasattr(app, "get") else None
    if cfg is None:
        try:
            from tesseract.integrations._channels_config import load_channels_config

            cfg = load_channels_config()
        except Exception:
            log.exception("daily_brief broadcast: channels config load failed")
            return False
    telegram_block = getattr(cfg, "telegram", None)
    if telegram_block is None:
        return False
    return bool(getattr(telegram_block, "brief_push", False))


def _latest_brief_payload(
    app: Any,
    *,
    date: str | None = None,
) -> dict[str, Any] | None:
    event_store = app.get("workspace_event_store") if hasattr(app, "get") else None
    if event_store is None:
        return None
    try:
        events = event_store.list_events(kinds=("daily_brief",), limit=5)
    except Exception:
        log.exception("daily_brief broadcast: list_events failed")
        return None
    for ev in events:
        payload = getattr(ev, "payload", None) or {}
        if not isinstance(payload, dict) or not payload.get("sections"):
            continue
        if date is not None and str(payload.get("date") or "") != date:
            continue
        return payload
    return None


def _resolve_telegram_state_dir() -> Path:
    import os

    return (
        Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
        / "telegram"
    )


class _TelegramAPISender:
    def __init__(self, api: Any) -> None:
        self._api = api

    async def send_text(self, *, chat_ref: str, text: str) -> None:
        from tesseract.integrations.telegram.api import TelegramAPIError
        from tesseract.integrations.telegram.chunker import chunk_for_telegram

        chat_id = int(chat_ref)
        for chunk in chunk_for_telegram(text or ""):
            try:
                await self._api.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                log.warning(
                    "daily_brief broadcast: TelegramAPI HTML send failed "
                    "for chat=%s; retrying plain",
                    chat_ref,
                )
                await self._api.send_message(
                    chat_id=chat_id,
                    text=_strip_html_tags(chunk),
                )


def _strip_html_tags(text: str) -> str:
    import re
    from html import unescape

    return unescape(re.sub(r"<[^>]+>", "", text))


__all__ = ["DailyBriefJob"]
