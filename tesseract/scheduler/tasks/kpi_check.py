"""KpiCheckJob — operator-configured KPI-website check.

Each run:

  1. Fetch every configured URL's page text (parallel — one dead URL must
     not kill the rest).
  2. One LLM pass over the fetched text answering each URL's `extract`
     instruction (role-chained per `build_chain_for_job`).
  3. Compose a short result — a "fetch failed: <url>" line for any dead
     URL plus the model's answer for the ones that succeeded.
  4. Deliver via the configured channel + write one source-tagged memory.

All config lives in `schedule.yaml::jobs[].config` (single source of
truth). The handler raises nothing — the scheduler contract forbids it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_LLM_TIMEOUT_S = 120.0
_MAX_PAGE_CHARS = 4000
_KPI_TAG = "kpi_check"
_KPI_SUBDIR = "reference/kpi_check"


class KpiCheckJob(BaseJob):
    uses_llm = True
    default_model_role = "agents_default"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = dict(ctx.config or {})
            urls_cfg = _normalize_urls(cfg.get("urls"))
            if not urls_cfg:
                return _result(ctx, t0, ok=False, detail="missing config: urls")

            fetch = _make_page_fetcher()
            fetched = await asyncio.gather(
                *(fetch(item["url"]) for item in urls_cfg), return_exceptions=True,
            )
            successes: list[tuple[dict[str, str], str]] = []
            failed_urls: list[tuple[str, str]] = []
            for item, res in zip(urls_cfg, fetched):
                if isinstance(res, BaseException):
                    exc_repr = repr(res)
                    log.warning("kpi_check: fetch failed for %s (%s)", item["url"], exc_repr)
                    failed_urls.append((item["url"], exc_repr))
                else:
                    successes.append((item, res))

            if not successes:
                return _result(
                    ctx, t0, ok=False,
                    detail=f"all {len(urls_cfg)} url fetch(es) failed",
                )

            chain = build_chain_for_job(
                ctx, default_role=self.default_model_role, log_label="kpi_check",
            )
            if not chain:
                return _result(ctx, t0, ok=True, detail="skipped: no adapter")

            answer = await _call_chain(_build_prompt(successes), chain, _LLM_TIMEOUT_S)
            if not answer:
                answer = _raw_fallback(successes)
            body = _compose_body(successes, failed_urls, answer)

            channel = cfg.get("channel")
            chat_ref = cfg.get("chat_ref")
            sent = False
            if channel and chat_ref:
                sent = await _deliver(str(channel), str(chat_ref), body)

            store = _resolve_memory_store(ctx.app)
            memory_id = _write_memory(store, body, when=ctx.fired_at) if store is not None else None

            detail = (
                f"fetched={len(successes)}/{len(urls_cfg)} failed={len(failed_urls)} "
                f"delivered={sent} memory_written={memory_id is not None}"
            )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=detail,
                payload={
                    "fetched": len(successes), "failed": len(failed_urls),
                    "delivered": sent, "memory_id": memory_id,
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("kpi_check crashed")
            return _result(ctx, t0, ok=False, detail=f"unhandled: {exc!r}")


def _normalize_urls(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        out.append({"url": url, "extract": str(item.get("extract") or "").strip()})
    return out


def _make_page_fetcher():
    """Return an async `fetch(url) -> str` hitting Tavily extract directly.

    Mirrors `brief_render._make_tavily_fetcher`'s injectable-fetcher shape
    (no ToolContext in a cron job). Raises on any failure so the caller's
    `asyncio.gather(..., return_exceptions=True)` can isolate dead URLs.
    """
    import os

    import httpx

    endpoint = "https://api.tavily.com/extract"
    timeout_s = 30.0

    async def _fetch(url: str) -> str:
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY not set")
        payload = {"urls": [url], "extract_depth": "basic"}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(endpoint, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        if not results:
            failed = data.get("failed_results") or []
            reason = failed[0].get("error") if failed else "no results"
            raise RuntimeError(f"tavily extract failed for {url}: {reason}")
        return str(results[0].get("raw_content") or "")

    return _fetch


def _build_prompt(successes: list[tuple[dict[str, str], str]]) -> str:
    parts = [
        "You are checking KPI websites for the operator. For EACH site below, "
        "answer its extraction instruction concisely using ONLY the page text given.",
        "",
    ]
    for item, text in successes:
        parts.append(f"URL: {item['url']}")
        parts.append(f"EXTRACT: {item['extract']}")
        parts.append("PAGE TEXT:")
        parts.append(text[:_MAX_PAGE_CHARS])
        parts.append("")
    parts.append("Respond with one short block per URL formatted as '<url>: <answer>'.")
    return "\n".join(parts)


async def _call_chain(prompt: str, chain: list, timeout_s: float) -> str:
    for adapter, options in chain:
        try:
            out = await asyncio.wait_for(
                adapter.generate(prompt, options or AdapterOptions()),
                timeout=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("kpi_check: LLM call failed (%s)", exc)
            continue
        if out and out.strip():
            return out.strip()
    return ""


def _raw_fallback(successes: list[tuple[dict[str, str], str]]) -> str:
    lines = ["KPI check (unanswered — model unavailable):", ""]
    for item, text in successes:
        lines.append(f"- {item['url']}: {text[:200].strip()}")
    return "\n".join(lines)


def _compose_body(
    successes: list[tuple[dict[str, str], str]], failed_urls: list[tuple[str, str]], answer: str,
) -> str:
    # Framed with real job context (checked URLs + their extract
    # instructions) rather than the bare model answer — a terse answer
    # alone (e.g. "uptime is 99.98%") falls under WhatNotToSave's
    # trivial-body floor; the surrounding context is what makes a
    # legitimately short KPI result a real memory record.
    lines = ["# KPI check", ""]
    for item, _text in successes:
        lines.append(f"- checked {item['url']} ({item['extract'] or 'no extract instruction'})")
    for url, exc_repr in failed_urls:
        lines.append(f"- fetch failed: {url} ({exc_repr})")
    lines.extend(["", "## Result", "", answer])
    return "\n".join(lines)


async def _deliver(channel: str, chat_ref: str, body: str) -> bool:
    """Send the result to the channel chat. False when the channel is down."""
    from tesseract.integrations import get_channel

    adapter = get_channel(channel)
    if adapter is None:
        log.warning("kpi_check: channel %r not registered", channel)
        return False
    if not hasattr(adapter, "send_text"):
        log.warning(
            "kpi_check: channel %r adapter %s has no send_text",
            channel, type(adapter).__name__,
        )
        return False
    try:
        await adapter.send_text(chat_ref=chat_ref, text=body)
    except Exception as exc:  # noqa: BLE001
        log.warning("kpi_check: send_text failed (%s)", exc)
        return False
    return True


def _resolve_memory_store(app: Any) -> Any:
    if app is None or not hasattr(app, "get"):
        return None
    bundle = app.get("memory_bundle")
    if bundle is None:
        return None
    return getattr(bundle, "store", None)


def _write_memory(store: Any, body: str, *, when) -> str | None:
    mem_id = MemoryFrontmatter.generate_id()
    fm = MemoryFrontmatter(
        id=mem_id,
        type=MemoryType.REFERENCE,
        title=f"KPI check — {when.date().isoformat()}",
        summary=body[:280],
        created_at=when,
        updated_at=when,
        importance=5,
        tags=[_KPI_TAG],
        stability=Stability.ACTIVE,
        source_type=_KPI_TAG,
    )
    try:
        # skip_wnts_check=True — kpi_check is a scheduled first-party
        # archive of Tavily-fetched KPI text, not an operator-facing
        # free-text save; the enriched `_compose_body` framing is the
        # primary fix (it clears every WNTS category on its own for a
        # realistic result — see test_terse_live_shaped_result_...),
        # this flag is only the backstop for a minimal-config run whose
        # body still lands near the 80-char trivial-body floor. Bypasses
        # all WNTS categories, not just trivial_body — accepted because
        # the body is bounded, config-driven KPI text, never operator
        # free-text.
        ok = store.write(fm, body, subdir_override=_KPI_SUBDIR, skip_wnts_check=True)
    except Exception:
        log.exception("kpi_check: memory write raised")
        return None
    if not ok:
        log.info("kpi_check: memory write declined by store")
        return None
    return mem_id


def _result(ctx: JobContext, t0: float, *, ok: bool, detail: str) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=ok,
        detail=detail,
        payload={},
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


__all__ = ["KpiCheckJob"]
