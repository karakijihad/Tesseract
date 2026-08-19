"""ScheduledTaskJob — the generic lean recurring-task primitive.

This is the handler the assistant points `schedule_create` at for *any* repetitive
"interpret → (optionally search) → filter → deliver" request that does NOT
need to be a mission. A mission (`WakeJob`) drags in a planner + an
operator approval gate + multi-step worker/verifier DAG — that machinery
is for one-off, plan-and-build-something goals. A recurring task that runs
the same shape every tick (summarize today's X, watch Y, digest Z and
message me) is just a cron job with an LLM step inside it. That's this.

Using an LLM does NOT make something a mission: the model is one step in
the job, not a planned graph. Per fire:

  1. (optional) Tavily-search the configured ``queries`` for fresh
     grounding — cost-capped, deduped by url. Omit ``queries`` for a
     pure-LLM task.
  2. Run the operator's ``prompt`` (plus any fetched sources) through the
     role's adapter chain.
  3. Deliver the result straight to the configured channel chat via
     ``send_text`` — Telegram today, not the Mirror workspace.

Delivery is fail-soft: raw sources are sent if the model is unavailable
but search succeeded; a pure-LLM task with no model returns ``ok=False``
without spamming the chat. The handler never raises — scheduler contract.

All config lives in ``schedule.yaml`` (single source of truth). The
specialized ``DailyJobSearchJob`` is a sibling of this generic job.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.kernel.tools.brief_render import _make_tavily_fetcher
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.tasks._archive import archive_run
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_DEFAULT_MAX_TAVILY_CALLS = 8
_DEFAULT_MAX_RESULTS = 5
_DEFAULT_LLM_TIMEOUT_S = 120.0  # floor; per-job override via config `llm_timeout_s`


class ScheduledTaskJob(BaseJob):
    uses_llm = True
    default_model_role = "agents_default"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = dict(ctx.config or {})
            prompt = str(cfg.get("prompt") or "").strip()
            chat_ref = cfg.get("chat_ref")
            if not prompt:
                return _result(ctx, t0, ok=False, detail="missing config: prompt")
            if not chat_ref:
                return _result(ctx, t0, ok=False, detail="missing config: chat_ref")
            channel = str(cfg.get("channel") or "telegram")
            chat_ref = str(chat_ref)
            title = str(cfg.get("title") or "").strip()

            hits = await _maybe_search(cfg)
            chain = build_chain_for_job(
                ctx, default_role=self.default_model_role, log_label="scheduled_task",
            )
            timeout_s = float(cfg.get("llm_timeout_s") or _DEFAULT_LLM_TIMEOUT_S)
            body = await _compose(chain, prompt, hits, timeout_s)
            if body is None:
                return _result(
                    ctx, t0, ok=False,
                    detail="no model available and no search grounding — nothing to deliver",
                    payload={"hits": len(hits)},
                )
            if title:
                body = f"{title}\n\n{body}"

            # Archive before delivery so the run is retrievable even if the
            # channel is down (memory-store/scheduled/<job_name>/<date>.md).
            archived = archive_run(
                ctx.job_name, body, ctx.fired_at, channel=channel, chat_ref=chat_ref,
            )
            archived_str = str(archived) if archived else None
            sent = await _deliver(channel, chat_ref, body)
            if not sent:
                return _result(
                    ctx, t0, ok=False,
                    detail=f"channel {channel!r} unavailable — result not delivered "
                    f"(archived={archived_str})",
                    payload={"hits": len(hits), "archived": archived_str},
                )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"ran task, delivered to {channel}:{chat_ref} (grounding hits={len(hits)})",
                payload={"hits": len(hits), "channel": channel, "archived": archived_str},
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("scheduled_task crashed")
            return _result(ctx, t0, ok=False, detail=f"unhandled: {exc!r}")


async def _maybe_search(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Tavily grounding when ``queries`` is set; empty list otherwise."""
    queries = [str(q).strip() for q in (cfg.get("queries") or []) if str(q).strip()]
    if not queries:
        return []
    cap = int(cfg.get("max_tavily_calls") or _DEFAULT_MAX_TAVILY_CALLS)
    queries = queries[:cap]
    fetch = _make_tavily_fetcher(None)  # no ToolContext in cron — same as brief_render
    options = {
        "max_results": int(cfg.get("max_results_per_query") or _DEFAULT_MAX_RESULTS),
        "exclude_domains": list(cfg.get("exclude_domains") or []),
        "topic": str(cfg.get("search_topic") or "general"),
    }
    results = await asyncio.gather(
        *(fetch(q, options) for q in queries), return_exceptions=True,
    )
    seen: set[str] = set()
    hits: list[dict[str, Any]] = []
    for res in results:
        if isinstance(res, BaseException) or not res:
            continue
        for hit in res:
            url = str(hit.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            hits.append(hit)
    return hits


async def _compose(
    chain: list, prompt: str, hits: list[dict[str, Any]], timeout_s: float,
) -> str | None:
    """LLM result, falling back to raw sources. None when nothing can be produced."""
    full_prompt = prompt
    if hits:
        sources = "\n".join(
            f"- {str(h.get('title') or '').strip()} | {str(h.get('url') or '').strip()} | "
            f"{str(h.get('content') or '').strip().replace(chr(10), ' ')[:300]}"
            for h in hits
        )
        full_prompt = (
            f"{prompt}\n\n"
            "Use these freshly-fetched sources where relevant; cite the URLs. "
            "Produce a concise, chat-ready message.\n\n"
            f"SOURCES:\n{sources}\n"
        )
    for adapter, options in chain:
        try:
            out = await asyncio.wait_for(
                adapter.generate(full_prompt, options or AdapterOptions()),
                timeout=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("scheduled_task: LLM call failed (%s)", exc)
            continue
        if out and out.strip():
            return out.strip()
    # No model: deliver the raw sources if we have any, else give up.
    if hits:
        return _raw_fallback(hits)
    return None


def _raw_fallback(hits: list[dict[str, Any]]) -> str:
    lines = ["(model unavailable — raw sources):", ""]
    for h in hits[:15]:
        title = str(h.get("title") or "").strip() or "(untitled)"
        lines.append(f"• {title}\n  {str(h.get('url') or '').strip()}")
    return "\n".join(lines)


async def _deliver(channel: str, chat_ref: str, body: str) -> bool:
    from tesseract.integrations import get_channel

    adapter = get_channel(channel)
    if adapter is None:
        log.warning("scheduled_task: channel %r not registered", channel)
        return False
    if not hasattr(adapter, "send_text"):
        log.warning(
            "scheduled_task: channel %r adapter %s has no send_text",
            channel, type(adapter).__name__,
        )
        return False
    try:
        await adapter.send_text(chat_ref=chat_ref, text=body)
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduled_task: send_text failed (%s)", exc)
        return False
    return True


def _result(
    ctx: JobContext, t0: float, *, ok: bool, detail: str, payload: dict | None = None,
) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=ok,
        detail=detail,
        payload=payload or {},
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )
