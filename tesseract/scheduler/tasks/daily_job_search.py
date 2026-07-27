"""DailyJobSearchJob — daily 09:00 job-posting sweep delivered to a chat.

A *lean* scheduled job, deliberately NOT a mission. The mission path
(``WakeTarsJob``) drags in a planner + an operator approval gate +
worker/verifier steps; for "search and send a shortlist every morning"
none of that applies, and the approval gate actively breaks autonomy
(it stalls every run waiting for the operator). This handler instead
does the work directly and pushes the result, exactly like
``DailyBriefJob``:

  1. Build the LLM adapter chain for the configured role.
  2. Fan out Tavily searches (cost-capped) for the operator's target
     roles × locations, plus any operator-supplied queries.
  3. Ask the model to rank/filter the hits against the operator's CV
     and the compensation floor, and format a concise shortlist.
  4. Deliver it deterministically to the configured channel chat
     (Telegram today) via the adapter's ``send_text`` — NOT the Mirror
     workspace, so the operator actually receives it where they are.

Delivery never silently no-ops: if the LLM chain is unavailable the raw
deduped hits are sent; if zero hits surface a short "nothing today"
note is sent so the operator knows the job ran.

All config lives in ``schedule.yaml`` (single source of truth). The
handler raises nothing — the scheduler contract forbids it.
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

_DEFAULT_MAX_TAVILY_CALLS = 12
_DEFAULT_MAX_RESULTS = 5
_LLM_TIMEOUT_S = 120.0
_DEFAULT_LOCATIONS: tuple[str, ...] = ()
_DEFAULT_ROLES = (
    "senior engineer",
    "engineering manager",
    "startup CTO",
    "tech consultant P.IVA",
    "AI agents engineer",
)


class DailyJobSearchJob(BaseJob):
    uses_llm = True
    default_model_role = "agents_default"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = dict(ctx.config or {})
            channel = str(cfg.get("channel") or "telegram")
            chat_ref = cfg.get("chat_ref")
            if not chat_ref:
                return _result(ctx, t0, ok=False, detail="missing config: chat_ref")
            chat_ref = str(chat_ref)

            queries = _build_queries(cfg)
            max_results = int(cfg.get("max_results_per_query") or _DEFAULT_MAX_RESULTS)
            exclude_domains = list(cfg.get("exclude_domains") or ["linkedin.com"])

            hits = await _collect_hits(queries, max_results, exclude_domains)
            chain = build_chain_for_job(
                ctx, default_role=self.default_model_role, log_label="daily_job_search",
            )
            body = await _render_body(chain, hits, cfg)

            # Archive before delivery so we keep the record even if the
            # channel is down (retrievable under memory-store/scheduled/).
            archived = archive_run(
                ctx.job_name, body, ctx.fired_at, channel=channel, chat_ref=chat_ref,
            )
            sent = await _deliver(channel, chat_ref, body)
            duration_ms = (time.monotonic() - t0) * 1000.0
            archived_str = str(archived) if archived else None
            if not sent:
                return _result(
                    ctx, t0, ok=False,
                    detail=f"channel {channel!r} unavailable — shortlist not delivered "
                    f"(archived={archived_str})",
                    payload={"hits": len(hits), "queries": len(queries), "archived": archived_str},
                )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"sent {len(hits)} hit(s) over {len(queries)} query(ies) to {channel}:{chat_ref}",
                payload={
                    "hits": len(hits), "queries": len(queries),
                    "channel": channel, "archived": archived_str,
                },
                duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("daily_job_search crashed")
            return _result(ctx, t0, ok=False, detail=f"unhandled: {exc!r}")


def _build_queries(cfg: dict[str, Any]) -> list[str]:
    """Operator-supplied ``queries`` win; otherwise derive roles × locations.

    Truncated to ``max_tavily_calls`` so a wide profile can't blow the
    per-run search budget.
    """
    cap = int(cfg.get("max_tavily_calls") or _DEFAULT_MAX_TAVILY_CALLS)
    explicit = [str(q).strip() for q in (cfg.get("queries") or []) if str(q).strip()]
    if explicit:
        return explicit[:cap]
    locations = [str(x) for x in (cfg.get("locations") or _DEFAULT_LOCATIONS)]
    roles_raw = cfg.get("target_roles")
    if isinstance(roles_raw, str):
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    elif isinstance(roles_raw, (list, tuple)):
        roles = [str(r).strip() for r in roles_raw if str(r).strip()]
    else:
        roles = list(_DEFAULT_ROLES)
    built = [f"{role} jobs {loc}" for loc in locations for role in roles]
    return built[:cap]


async def _collect_hits(
    queries: list[str], max_results: int, exclude_domains: list[str],
) -> list[dict[str, Any]]:
    """Fan out every query concurrently, then dedupe hits by URL."""
    fetch = _make_tavily_fetcher(None)  # no ToolContext in cron — same as DailyBriefJob
    # "general" index: job postings live on the open web, not Tavily's "news" topic.
    options = {
        "max_results": max_results,
        "exclude_domains": exclude_domains,
        "topic": "general",
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


async def _render_body(chain: list, hits: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    """LLM ranks/filters the hits; falls back to raw hits if no model is up."""
    if not hits:
        return "🔎 Daily job search: no matching postings surfaced today."
    if not chain:
        return _raw_fallback(hits)
    prompt = _build_prompt(hits, cfg)
    for adapter, options in chain:
        try:
            out = await asyncio.wait_for(
                adapter.generate(prompt, options or AdapterOptions()),
                timeout=_LLM_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("daily_job_search: LLM call failed (%s)", exc)
            continue
        if out and out.strip():
            return out.strip()
    return _raw_fallback(hits)


def _build_prompt(hits: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    comp_floor = cfg.get("comp_floor_eur") or 0
    cv = str(cfg.get("cv_summary") or "").strip() or "(no CV summary configured)"
    lines = []
    for h in hits:
        title = str(h.get("title") or "").strip()
        url = str(h.get("url") or "").strip()
        snippet = str(h.get("content") or "").strip().replace("\n", " ")[:300]
        lines.append(f"- {title} | {url} | {snippet}")
    raw = "\n".join(lines)
    return (
        "You are screening job postings for this candidate.\n\n"
        f"CANDIDATE CV SUMMARY:\n{cv}\n\n"
        "TASK: From the RAW POSTINGS below, keep only roles that genuinely fit "
        "the candidate and target senior / manager / startup-CTO / tech-consultant "
        f"(incl. P.IVA) / AI-agent profiles with total comp at or above €{comp_floor}. "
        "Drop duplicates, recruiters' spam, and anything clearly junior or off-profile. "
        "Output a concise Telegram-friendly shortlist, one posting per block:\n"
        "• <Title> — <Company> — <Location>\n  <URL>\n  Fit: <one line>\n\n"
        "Start with a one-line header. If nothing qualifies, say so plainly.\n\n"
        f"RAW POSTINGS:\n{raw}\n"
    )


def _raw_fallback(hits: list[dict[str, Any]]) -> str:
    lines = ["🔎 Daily job search (unranked — model unavailable):", ""]
    for h in hits[:15]:
        title = str(h.get("title") or "").strip() or "(untitled)"
        url = str(h.get("url") or "").strip()
        lines.append(f"• {title}\n  {url}")
    return "\n".join(lines)


async def _deliver(channel: str, chat_ref: str, body: str) -> bool:
    """Send the shortlist to the channel chat. False when the channel is down."""
    from tesseract.integrations import get_channel

    adapter = get_channel(channel)
    if adapter is None:
        log.warning("daily_job_search: channel %r not registered", channel)
        return False
    if not hasattr(adapter, "send_text"):
        log.warning(
            "daily_job_search: channel %r adapter %s has no send_text",
            channel, type(adapter).__name__,
        )
        return False
    try:
        await adapter.send_text(chat_ref=chat_ref, text=body)
    except Exception as exc:  # noqa: BLE001
        log.warning("daily_job_search: send_text failed (%s)", exc)
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
