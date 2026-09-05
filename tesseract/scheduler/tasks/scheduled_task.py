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
  3. Hand the result to the one notify path as ``scheduled_task_result``.
     Where it goes is the row's own ``delivery`` if it states one and
     ``routing.yaml`` otherwise, and WHO hears it is the channel's operator
     roster. **This job names no channel and no chat, deliberately.** A
     destination in a handler's own config block is a destination compiled
     into the sender: it cannot be re-pointed without editing the row, a
     second channel cannot be added to it at all, and a chat id in a data
     file addresses one conversation that may no longer exist.

A row routed nowhere on purpose is not a failed run. A row that was routed
somewhere and reached nobody is.

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

from tesseract import http_client
from tesseract.kernel.adapters.base import AdapterOptions
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
    # A CHAIN, not a role. `agents_default` and `subagents_default` are seats
    # that also serve `invoke_agent`, so sharing one meant this job's model and
    # its spend moved whenever an agent was re-pointed. Naming the chain
    # directly severs that: what it spends bills to the entry, whose ceiling is
    # on its manifest entry.
    default_model_chain = "chain_1"
    # This handler is the one instantiated under many different operator-chosen
    # row names, so the row name is not an entry the ledger can key a ceiling
    # on. Every armed row bills here instead, which is where `roles.yaml`
    # declares the ceiling for work whose size is not known in advance.
    billing_entry = "scheduled_task"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = dict(ctx.config or {})
            prompt = str(cfg.get("prompt") or "").strip()
            if not prompt:
                return _result(ctx, t0, ok=False, detail="missing config: prompt")
            # What the operator called this row, and it heads the message.
            # With twenty rows armed, which one spoke is the first thing a
            # reader needs; the row's own name is the honest fallback.
            title = str(cfg.get("title") or "").strip() or ctx.job_name

            hits = await _maybe_search(cfg)
            chain = build_chain_for_job(
                ctx,
                default_role=None,
                default_chain=self.default_model_chain,
                log_label="scheduled_task",
            )
            timeout_s = float(cfg.get("llm_timeout_s") or _DEFAULT_LLM_TIMEOUT_S)
            body = await _compose(chain, prompt, hits, timeout_s)
            if body is None:
                return _result(
                    ctx, t0, ok=False,
                    detail="no model available and no search grounding — nothing to deliver",
                    payload={"hits": len(hits)},
                )
            # Archive before delivery so the run is retrievable even if every
            # channel is down (memory-store/scheduled/<job_name>/<date>.md).
            archived = archive_run(ctx.job_name, body, ctx.fired_at)
            archived_str = str(archived) if archived else None

            result = await _deliver(ctx, title, body)
            reached = [
                name for name, why in getattr(result, "by_channel", ()) if why == "sent"
            ]
            payload = {
                "hits": len(hits),
                "archived": archived_str,
                "sent": getattr(result, "sent", 0),
                "channels": reached,
            }
            # A row the operator routed nowhere did exactly what they asked.
            # Only a row that was routed somewhere and reached nobody has
            # failed, and the reason it gives is the notifier's own.
            if result is None:
                return _result(
                    ctx, t0, ok=False,
                    detail=f"nothing to deliver with (archived={archived_str})",
                    payload=payload,
                )
            if result.sent == 0 and result.reason != "no_destination":
                return _result(
                    ctx, t0, ok=False,
                    detail=f"reached nobody: {result.reason or 'unknown'} "
                    f"(archived={archived_str})",
                    payload=payload,
                )
            where = ", ".join(reached) or "nowhere, by your routing"
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"ran task, delivered to {where} (grounding hits={len(hits)})",
                payload=payload,
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("scheduled_task crashed")
            return _result(ctx, t0, ok=False, detail=f"unhandled: {exc!r}")


def _make_tavily_fetcher():
    """Return a Tavily fetcher that hits the API directly.

    ``TavilySearchTool`` collapses results into prose for the chat surface;
    a scheduled task needs the per-hit ``url`` to dedupe across queries, so
    this calls the same endpoint with the same auth and returns the
    structured ``results`` list. Any failure (missing key, timeout, non-200)
    yields ``[]`` and the caller continues to the next query.
    """
    import os

    import httpx

    endpoint = "https://api.tavily.com/search"
    timeout_s = 15.0

    async def _fetch(query: str, options: dict) -> list[dict]:
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            log.info("scheduled_task: TAVILY_API_KEY not set; skipping query %r", query)
            return []
        payload: dict[str, object] = {
            "query": query,
            "max_results": int(options.get("max_results", 5)),
            "search_depth": "basic",
            # The caller always names one; this default is only a floor.
            "topic": options.get("topic", "general"),
            "include_answer": False,
        }
        include = list(options.get("include_domains") or [])
        if include:
            payload["include_domains"] = include
        exclude = list(options.get("exclude_domains") or [])
        if exclude:
            payload["exclude_domains"] = exclude
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with http_client.async_client(timeout=timeout_s) as client:
                r = await client.post(endpoint, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            log.info("scheduled_task: tavily query %r failed (%s)", query, exc)
            return []
        if r.status_code != 200:
            log.info("scheduled_task: tavily %s for query %r", r.status_code, query)
            return []
        try:
            data = r.json()
        except ValueError:
            return []
        results = data.get("results") or []
        return [hit for hit in results if isinstance(hit, dict)]

    return _fetch


async def _maybe_search(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Tavily grounding when ``queries`` is set; empty list otherwise."""
    queries = [str(q).strip() for q in (cfg.get("queries") or []) if str(q).strip()]
    if not queries:
        return []
    cap = int(cfg.get("max_tavily_calls") or _DEFAULT_MAX_TAVILY_CALLS)
    queries = queries[:cap]
    fetch = _make_tavily_fetcher()
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


async def _deliver(ctx: JobContext, title: str, body: str) -> Any:
    """Hand the result to the one notify path, and let it decide where.

    Returns the ``NotifyResult``, or ``None`` when there is no notifier to
    hand it to at all: a boot with no channels, or a send that raised. That
    is not the same as being routed nowhere, and the caller tells them apart
    because one is the operator's decision and the other is a fault.
    """
    app = ctx.app
    notifier = app.get("outbound_notifier") if hasattr(app, "get") else None
    if notifier is None:
        log.warning("scheduled_task: no notifier, %s reached nobody", ctx.job_name)
        return None
    try:
        return await notifier.notify(
            "scheduled_task_result",
            {"title": title, "body": body},
            destinations=ctx.delivery,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduled_task: delivery failed (%s)", exc)
        return None


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
