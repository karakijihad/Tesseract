"""AutonomyStrategistJob — AU-23 — periodic initiative curator.

Cadence is NOT hardcoded here — `schedule.yaml::autonomy_strategist.cadence`
is the single source of truth (operator-tunable). The `lookback_days`
and `dedupe_window_days` knobs in the same schedule entry let the
operator align the substrate window with whatever cron expression they
choose.

Each tick:

1. Pre-fetches recent agenda outcomes, discovery leaves, vault deltas,
   failed workers, governor pauses, and operator-view presence over the
   configured lookback window via :func:`strategist.collect_inputs`.
2. Idle short-circuit when every input stream is empty — never bills
   the model on a quiet window.
3. Calls the ``autonomy_strategist`` role chain with the prompt built
   by :func:`strategist.build_prompt` (the prompt embeds the exact
   window range so the model isn't guessing).
4. Parses + filters + dedupes against the rolling ledger at
   ``<TESSERACT_HOME>/autonomy/strategist-seen.jsonl``.
5. Publishes each surviving initiative to the autonomy bus under
   ``AgendaSource.STRATEGIST``. AU-23 Session 2 mapper folds these
   into ``AgendaItem`` rows with an ``operator_review`` gate.
6. Writes a single ``strategist_summary`` workspace event so the batch
   lands in the operator's inbox + the next daily brief.
7. Appends each published initiative to the ledger so a re-fired tick
   doesn't duplicate within the dedup window.

Distinct from :class:`AutonomyHeartbeatJob`:
- heartbeat = reactive per-tick (minutes scale), one observation per
  event, no portfolio framing.
- strategist = deliberate low-cadence (days scale, operator-configured
  in schedule.yaml), portfolio framing, explicit success criteria +
  horizon_days, always operator-attended.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.autonomy.publishers import publish_to_bus
from tesseract.orchestrator.autonomy.strategist import (
    DEFAULT_DEDUPE_WINDOW_DAYS,
    DEFAULT_IDLE_LOOKBACK_DAYS,
    DEFAULT_MAX_INITIATIVES,
    DEFAULT_MIN_CONFIDENCE,
    Initiative,
    append_seen,
    build_prompt,
    collect_inputs,
    dedupe_against_ledger,
    filter_initiatives,
    initiative_key,
    parse_response,
    read_seen,
    seen_ledger_path,
)
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult
from tesseract.workspace_events.events import WorkspaceEvent

log = logging.getLogger(__name__)


DEFAULT_TIMEOUT_S = 60.0


class AutonomyStrategistJob(BaseJob):
    uses_llm = True
    default_model_role = "autonomy_strategist"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            now = ctx.fired_at
            ledger_path = _resolve_ledger_path(ctx)
            lookback_days = int(ctx.config.get("lookback_days") or DEFAULT_IDLE_LOOKBACK_DAYS)
            dedupe_window_days = int(
                ctx.config.get("dedupe_window_days") or DEFAULT_DEDUPE_WINDOW_DAYS
            )
            min_confidence = float(
                ctx.config.get("min_confidence") or DEFAULT_MIN_CONFIDENCE
            )
            max_initiatives = int(
                ctx.config.get("max_initiatives") or DEFAULT_MAX_INITIATIVES
            )

            inputs = collect_inputs(
                app=ctx.app,
                now=now,
                lookback_days=lookback_days,
                tesseract_home=_resolve_home_override(ctx),
            )

            if inputs.is_idle():
                return _ok(
                    ctx, t0,
                    detail="idle",
                    payload={
                        "initiatives_returned": 0,
                        "initiatives_published": 0,
                        "reason": "no_recent_activity",
                    },
                )

            chain = build_chain_for_job(
                ctx,
                default_role=AutonomyStrategistJob.default_model_role,
                log_label="autonomy_strategist",
            )
            if not chain:
                return _ok(
                    ctx, t0,
                    detail="role_unavailable",
                    payload={
                        "initiatives_returned": 0,
                        "initiatives_published": 0,
                    },
                )

            prompt = build_prompt(inputs)
            raw = await _call_with_fallback(prompt, chain, DEFAULT_TIMEOUT_S)
            parsed = parse_response(raw)
            kept = filter_initiatives(
                parsed,
                min_confidence=min_confidence,
                max_count=max_initiatives,
            )

            seen = read_seen(ledger_path, now=now, window_days=dedupe_window_days)
            fresh = dedupe_against_ledger(kept, seen=seen)

            published_initiatives: list[Initiative] = []
            ledger_failures = 0
            for initiative in fresh:
                # Ledger write first, then publish. Skip the publish
                # when the ledger write fails — emitting a bus event
                # without a confirmed dedup row would let the same
                # initiative re-fire on every tick of the dedup window.
                if not append_seen(ledger_path, initiative=initiative, when=now):
                    ledger_failures += 1
                    log.warning(
                        "autonomy_strategist: skipping publish for %s — "
                        "ledger append failed (path=%s)",
                        initiative.slug, ledger_path,
                    )
                    continue
                _publish(initiative, when=now)
                published_initiatives.append(initiative)
            published = len(published_initiatives)

            event_id = _emit_workspace_summary(
                ctx=ctx,
                initiatives=published_initiatives,
                now=now,
            )

            detail = (
                f"returned={len(parsed)} kept={len(kept)} "
                f"fresh={len(fresh)} published={published}"
            )
            if ledger_failures:
                detail += f" ledger_failures={ledger_failures}"
            return _ok(
                ctx, t0,
                detail=detail,
                payload={
                    "initiatives_returned": len(parsed),
                    "initiatives_kept": len(kept),
                    "initiatives_fresh": len(fresh),
                    "initiatives_published": published,
                    "ledger_failures": ledger_failures,
                    "slugs": [i.slug for i in published_initiatives],
                    "workspace_event_id": event_id,
                    "window_start": inputs.window_start_iso,
                    "window_end": inputs.window_end_iso,
                },
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("autonomy_strategist crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


# ── helpers ─────────────────────────────────────────────────────────


def _ok(ctx: JobContext, t0: float, *, detail: str, payload: dict[str, Any]) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=True,
        detail=detail,
        payload=payload,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


def _resolve_home_override(ctx: JobContext) -> Path | None:
    override = ctx.config.get("tesseract_home")
    return Path(override) if override else None


def _resolve_ledger_path(ctx: JobContext) -> Path:
    override = ctx.config.get("seen_path")
    if override:
        return Path(override)
    return seen_ledger_path(_resolve_home_override(ctx))


async def _call_with_fallback(
    prompt: str,
    chain: list[tuple[ModelAdapter, AdapterOptions]],
    timeout_s: float,
) -> str:
    for adapter, options in chain:
        label = f"{options.provider or '?'}/{options.model or '?'}"
        try:
            out = await asyncio.wait_for(
                adapter.generate(prompt, options),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("autonomy_strategist: %s timed out after %.1fs", label, timeout_s)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("autonomy_strategist: %s call failed (%s)", label, exc)
            continue
        if out and out.strip():
            return out
        log.warning("autonomy_strategist: %s returned empty", label)
    return ""


def _publish(initiative: Initiative, *, when: datetime) -> None:
    payload = {
        "slug": initiative.slug,
        "goal": initiative.goal,
        "rationale": initiative.rationale,
        "success_criteria": list(initiative.success_criteria),
        "suggested_risk_class": initiative.suggested_risk_class.value,
        "evidence": list(initiative.evidence),
        "confidence": initiative.confidence,
        "horizon_days": initiative.horizon_days,
        "emitted_at": when.isoformat(),
        "source_handler": "autonomy_strategist",
    }
    event_id = f"evt_strategist_{initiative_key(initiative)[:16]}"
    publish_to_bus(AgendaSource.STRATEGIST, payload, event_id=event_id)


def _emit_workspace_summary(
    *,
    ctx: JobContext,
    initiatives: list[Initiative],
    now: datetime,
) -> str | None:
    """Write one ``strategist_summary`` workspace event covering the whole
    batch. Returns the event id on success, ``None`` when the store isn't
    mounted (CLI-only runs) or the write failed."""
    if not initiatives:
        return None
    store = _resolve_event_store(ctx.app)
    if store is None:
        return None
    title = f"strategist — {len(initiatives)} initiative(s) this tick"
    summary_lines: list[str] = []
    body_blocks: list[str] = []
    for initiative in initiatives:
        summary_lines.append(f"• {initiative.slug} — {initiative.goal[:120]}")
        body_blocks.append(_format_initiative_block(initiative))
    summary = "\n".join(summary_lines)[:1200]
    event = WorkspaceEvent.new(
        kind="strategist_summary",
        source="strategist",
        title=title,
        summary=summary,
        payload={
            "emitted_at": now.isoformat(),
            "initiatives": [
                {
                    "slug": initiative.slug,
                    "goal": initiative.goal,
                    "rationale": initiative.rationale,
                    "success_criteria": list(initiative.success_criteria),
                    "suggested_risk_class": initiative.suggested_risk_class.value,
                    "evidence": list(initiative.evidence),
                    "confidence": initiative.confidence,
                    "horizon_days": initiative.horizon_days,
                }
                for initiative in initiatives
            ],
            "body_markdown": "\n\n".join(body_blocks),
        },
        priority=5,
        author_id="strategist",
        author_display="Autonomy strategist",
    )
    try:
        store.append_event(event)
    except Exception:
        log.exception("autonomy_strategist: workspace event append failed")
        return None
    return event.event_id


def _format_initiative_block(initiative: Initiative) -> str:
    lines = [
        f"### {initiative.slug}",
        "",
        f"**Goal.** {initiative.goal}",
        "",
        f"**Why.** {initiative.rationale}",
        "",
        "**Success.**",
    ]
    for criterion in initiative.success_criteria:
        lines.append(f"- {criterion}")
    lines.append("")
    lines.append(
        f"_risk={initiative.suggested_risk_class.value} "
        f"confidence={initiative.confidence:.2f} "
        f"horizon={initiative.horizon_days}d_"
    )
    if initiative.evidence:
        lines.append("")
        lines.append("Evidence: " + ", ".join(initiative.evidence))
    return "\n".join(lines)


def _resolve_event_store(app: Any) -> Any:
    if app is None or not hasattr(app, "get"):
        return None
    return app.get("workspace_event_store")


__all__ = ["AutonomyStrategistJob"]
