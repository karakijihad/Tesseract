"""ScoutJob — P7 Task 2b — identity-anchored discovery.

Domain-unbounded curiosity with a budget, not a feed reader. Each run:

  1. Feedback scan — previously published SCOUT items that reached an
     operator-terminal state (accepted/dispatched vs cancelled/rejected)
     and haven't been fed back yet each get one source-tagged memory, so
     the next run's recent-memory reader sees how past proposals landed.
  2. Query gen — ONE LLM call. TARS derives search queries from its own
     identity (SOUL.md/IDENTITY.md), the open agenda, recent memory, and
     that same accept/reject history. ``seed_topics`` (config) are hints,
     never an allowlist — the prompt says so explicitly.
  3. Sweep — the generated queries hit web search (Tavily) and any
     configured per-topic ``feeds`` URLs, in parallel, each source gated
     by its own circuit breaker (``scout_tavily``, ``scout_<domain>``). A
     tripped breaker skips that source this run; other sources continue.
  4. Dedup vs the persistent seen-store, then a deterministic pre-filter
     (drop malformed/empty, cap the candidate count).
  5. Idle short-circuit — zero fresh candidates after dedup/pre-filter
     returns ``ok=True, detail="idle"`` BEFORE the evaluation LLM call.
  6. Evaluation — ONE LLM call, identity-anchored ("worth *our* time?"),
     picking at most ``max_proposals_per_run`` candidates, each with an
     explicit "why us / why now" line. A model-swap finding carries the
     exact providers.yaml/roles.yaml diff as TEXT — never applied here.
  7. Publish — one ``AutonomyEvent(source=SCOUT)`` per pick via
     ``publish_to_bus``; the seen-store is updated with every fresh
     candidate considered this run (published or evaluated-and-dropped),
     and published events get a row in the horizon ledger that
     ``ScoutReaperJob`` reads to expire unacted proposals.

Delivery is the agenda only — this job never sends channel messages,
writes config files, or registers tools. All config lives in
``schedule.yaml::jobs[].config``; the handler raises nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator

from tesseract.context.circuit_breaker import CircuitBreaker
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import AgendaItem, AgendaSource, AgendaStatus
from tesseract.orchestrator.autonomy.paths import agenda_archive_dir
from tesseract.orchestrator.autonomy.publishers import publish_to_bus
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_LLM_TIMEOUT_S = 60.0
_MAX_CANDIDATES_FOR_EVAL = 20
_MAX_FEED_ENTRIES = 20
_MAX_AGENDA_CONTEXT = 8
_MAX_MEMORY_CONTEXT = 8
_MAX_HISTORY_CONTEXT = 10
_IDENTITY_SNIPPET_CHARS = 2000
_SCOUT_TAG = "scout"
_SCOUT_FEEDBACK_SUBDIR = "conscience/scout"

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_ANCHOR_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


class _QueryGenResponse(BaseModel):
    queries: list[str] = Field(default_factory=list)

    @field_validator("queries")
    @classmethod
    def _trim(cls, v: list[Any]) -> list[str]:
        return [s[:200] for q in v if (s := str(q).strip())]


class _EvalPick(BaseModel):
    candidate_index: int = Field(ge=0)
    why_us_why_now: str = Field(min_length=5, max_length=800)
    diff_text: str = Field(default="")


class _EvalResponse(BaseModel):
    picks: list[_EvalPick] = Field(default_factory=list)


class ScoutJob(BaseJob):
    uses_llm = True
    default_model_role = "autonomy_scout"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = dict(ctx.config or {})
            max_searches = cfg.get("max_searches_per_run")
            if max_searches is None:
                return _result(ctx, t0, ok=False, detail="missing config: max_searches_per_run")
            max_proposals = cfg.get("max_proposals_per_run")
            if max_proposals is None:
                return _result(ctx, t0, ok=False, detail="missing config: max_proposals_per_run")
            staleness_days = cfg.get("staleness_days")
            if staleness_days is None:
                return _result(ctx, t0, ok=False, detail="missing config: staleness_days")
            max_searches = int(max_searches)
            max_proposals = int(max_proposals)
            staleness_days = int(staleness_days)

            now = ctx.fired_at
            topics, feed_urls = _normalize_seed_topics(cfg.get("seed_topics"))

            store = _resolve_agenda_store(ctx)
            history_items = _scan_terminal_scout_items(store)
            fedback_path = _resolve_fedback_path(ctx)
            fed_back = _run_feedback_scan(ctx, history_items, fedback_path, when=now)

            chain = build_chain_for_job(ctx, default_role=self.default_model_role, log_label="scout")
            if not chain:
                return _result(ctx, t0, ok=True, detail=f"skipped: no adapter fed_back={fed_back}")

            identity = _read_identity_snippet()
            agenda_snapshot = _summarize_agenda(store)
            memory_snapshot = _collect_recent_memory(ctx)
            history_lines = _summarize_history(history_items)

            query_prompt = _build_query_prompt(
                identity=identity, agenda=agenda_snapshot, memory=memory_snapshot,
                history=history_lines, topics=topics, max_queries=max_searches,
            )
            raw_queries = await _call_chain(query_prompt, chain, _LLM_TIMEOUT_S)
            queries = _parse_envelope(raw_queries, _QueryGenResponse).queries[:max_searches]

            candidates, sweep_notes, all_failed, attempted = await _sweep(queries, feed_urls)

            if all_failed:
                detail = f"all sources failed fed_back={fed_back}"
                if sweep_notes:
                    detail += ": " + "; ".join(sweep_notes)
                return _result(ctx, t0, ok=False, detail=detail)

            total_sources = len(queries) + len(feed_urls)
            if total_sources > 0 and attempted == 0:
                # Every configured source is breaker-open — a genuinely
                # healthy "idle" run has at least one source to try. Surfacing
                # this as ok=False (not a quiet "idle") keeps on_failure:
                # alert firing every day the outage persists, instead of the
                # job going silently dark behind an ok=True detail string
                # (2026-07-06 review — a tripped breaker never self-heals
                # since nothing ever calls record_success() on a source that
                # is never attempted again).
                detail = f"all sources breaker-open fed_back={fed_back}"
                if sweep_notes:
                    detail += ": " + "; ".join(sweep_notes)
                return _result(ctx, t0, ok=False, detail=detail)

            seen_path = _resolve_seen_path(ctx)
            seen = _read_seen(seen_path)
            fresh = _dedup_and_prefilter(candidates, seen)

            if not fresh:
                detail = f"idle queries={len(queries)} candidates={len(candidates)} fed_back={fed_back}"
                if sweep_notes:
                    detail += "; " + "; ".join(sweep_notes)
                return _result(ctx, t0, ok=True, detail=detail)

            picks = await _evaluate(chain, fresh, identity=identity, max_proposals=max_proposals)
            published_ids = _publish_picks(picks, fresh, when=now)

            _update_seen(seen_path, [c["_key"] for c in fresh], when=now)
            if published_ids:
                horizon_path = _resolve_horizon_path(ctx)
                _record_horizon(horizon_path, event_ids=published_ids, staleness_days=staleness_days, when=now)

            detail = (
                f"queries={len(queries)} candidates={len(candidates)} fresh={len(fresh)} "
                f"published={len(published_ids)} fed_back={fed_back}"
            )
            if sweep_notes:
                detail += "; " + "; ".join(sweep_notes)
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=detail,
                payload={
                    "queries": len(queries), "candidates": len(candidates), "fresh": len(fresh),
                    "published": len(published_ids), "fed_back": fed_back,
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("scout crashed")
            return _result(ctx, t0, ok=False, detail=f"unhandled: {exc!r}")


# ── Config ──────────────────────────────────────────────────────────


def _normalize_seed_topics(raw: Any) -> tuple[list[str], list[str]]:
    """``seed_topics`` items are hints, never an allowlist. Each item is
    either a plain string or ``{"topic": str, "feeds": [url, ...]}``."""
    topics: list[str] = []
    feeds: list[str] = []
    for item in raw or []:
        if isinstance(item, str):
            t = item.strip()
            if t:
                topics.append(t)
        elif isinstance(item, dict):
            t = str(item.get("topic") or "").strip()
            if t:
                topics.append(t)
            for f in item.get("feeds") or []:
                fu = str(f).strip()
                if fu:
                    feeds.append(fu)
    return topics, feeds


# ── Identity / context readers (reuse existing read paths — no new store) ──


def _read_identity_snippet() -> str:
    from tesseract.paths import workspace_dir
    ws = workspace_dir()
    parts: list[str] = []
    for name in ("SOUL.md", "IDENTITY.md"):
        path = ws / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parts.append(_strip_frontmatter(text))
    combined = "\n\n".join(p for p in parts if p.strip())
    return combined[:_IDENTITY_SNIPPET_CHARS]


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _resolve_agenda_store(ctx: JobContext) -> AgendaStore | None:
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        live = app.get("agenda_store")
        if live is not None:
            return live
    try:
        return AgendaStore()
    except Exception:  # noqa: BLE001
        log.exception("scout: AgendaStore() init failed")
        return None


def _summarize_agenda(store: AgendaStore | None) -> list[str]:
    if store is None:
        return []
    try:
        items = store.list_active()
    except Exception:  # noqa: BLE001
        return []
    items = sorted(items, key=lambda i: i.created_at, reverse=True)[:_MAX_AGENDA_CONTEXT]
    return [f"- {i.goal} [{i.status.value}]" for i in items]


def _scan_terminal_scout_items(store: AgendaStore | None) -> list[AgendaItem]:
    """Walk the archive for terminal SCOUT items. Mirrors
    ``governor.py::_collect_archive_items_in_window``'s archive walk."""
    if store is None:
        return []
    out: list[AgendaItem] = []
    root = agenda_archive_dir()
    if not root.exists():
        return out
    try:
        for month_dir in sorted(root.iterdir()):
            if not month_dir.is_dir():
                continue
            for child in sorted(month_dir.iterdir()):
                if child.suffix != ".json":
                    continue
                try:
                    raw = json.loads(child.read_text(encoding="utf-8"))
                    item = AgendaItem.model_validate(raw)
                except Exception:  # noqa: BLE001
                    continue
                if item.source is AgendaSource.SCOUT:
                    out.append(item)
    except OSError:
        log.exception("scout: archive walk failed")
    out.sort(key=lambda i: i.updated_at, reverse=True)
    return out


def _feedback_decision(item: AgendaItem) -> str | None:
    """Map a terminal SCOUT item to a feedback decision, or ``None`` if it
    isn't an operator decision at all.

    ABANDONED (``ScoutReaperJob``'s own staleness timeout — nobody looked
    at it) and SUPERSEDED (the vetter's dedup merge, ``by="kernel"``) are
    not operator decisions; counting them as "rejected" would corrupt the
    exact accept/reject signal this loop exists to sharpen, so they never
    produce a decision. CANCELLED is reachable from two different actors:
    the vetter's own low-value reject (``autonomy_vetter.py::_reject``,
    ``by="kernel"``) and an actual operator rejection (e.g.
    ``mirror/server/routes/agenda.py``, ``by="operator"``) — only the
    latter is real feedback, distinguished via the last status transition's
    ``by`` field."""
    if item.status == AgendaStatus.DONE:
        return "accepted"
    if item.status == AgendaStatus.CANCELLED:
        last = item.status_history[-1] if item.status_history else None
        if last is not None and last.by == "operator":
            return "rejected"
        return None
    return None


def _summarize_history(items: list[AgendaItem]) -> list[str]:
    lines: list[str] = []
    for item in items:
        decision = _feedback_decision(item)
        if decision is None:
            continue
        lines.append(f"- {decision}: {item.goal}")
        if len(lines) >= _MAX_HISTORY_CONTEXT:
            break
    return lines


def _collect_recent_memory(ctx: JobContext) -> list[str]:
    """Recent memory writes, mirroring ``autonomy_heartbeat._collect_memory_rows``
    minus the cursor (scout has no cursor — it's driven by the seen-store
    instead)."""
    store_dir = _resolve_memory_store_dir(ctx)
    if store_dir is None:
        return []
    writes_path = store_dir / "events" / "writes.jsonl"
    if not writes_path.exists():
        return []
    rows: list[str] = []
    try:
        with writes_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        log.exception("scout: writes.jsonl read failed")
        return []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") != "written":
            continue
        title = str(row.get("title") or "")[:200]
        if title.startswith("scout feedback"):
            continue
        rows.append(f"- [{row.get('timestamp') or ''}] {title}")
        if len(rows) >= _MAX_MEMORY_CONTEXT:
            break
    return rows


def _resolve_memory_store_dir(ctx: JobContext) -> Path | None:
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        tdir = app.get("tesseract_dir")
        if tdir is not None:
            return Path(tdir) / "memory-store"
        bundle = app.get("memory_bundle")
        store = getattr(bundle, "store", None)
        store_dir = getattr(store, "store_dir", None)
        if store_dir is not None:
            return Path(store_dir)
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "memory-store"


# ── Feedback scan (accept/reject → memory) ─────────────────────────────


def _run_feedback_scan(
    ctx: JobContext, history_items: list[AgendaItem], fedback_path: Path, *, when: datetime,
) -> int:
    if not history_items:
        return 0
    already = _read_id_ledger(fedback_path)
    mem_store = _resolve_memory_store(ctx.app)
    count = 0
    for item in history_items:
        if item.id in already:
            continue
        decision = _feedback_decision(item)
        if decision is None:
            # ABANDONED / SUPERSEDED / vetter-CANCELLED — not an operator
            # decision. Left off the ledger too (cheap to re-check next
            # run against a small archive) so a later real transition on
            # the same item id is never masked by an early skip.
            continue
        if mem_store is not None:
            _write_feedback_memory(mem_store, item, decision, when=when)
        _append_id_ledger(fedback_path, item_id=item.id, when=when)
        count += 1
    return count


def _resolve_memory_store(app: Any) -> Any:
    if app is None or not hasattr(app, "get"):
        return None
    bundle = app.get("memory_bundle")
    if bundle is None:
        return None
    return getattr(bundle, "store", None)


def _write_feedback_memory(store: Any, item: AgendaItem, decision: str, *, when: datetime) -> str | None:
    mem_id = MemoryFrontmatter.generate_id()
    title = f"scout feedback — {decision}: {item.goal[:80].strip()}"
    body = (
        "# Scout feedback\n\n"
        f"- item_id: {item.id}\n"
        f"- decision: {decision}\n"
        f"- status: {item.status.value}\n"
        f"- emitted_at: {when.isoformat()}\n\n"
        "## Goal\n\n"
        f"{item.goal}\n"
    )
    fm = MemoryFrontmatter(
        id=mem_id,
        type=MemoryType.CONSCIENCE,
        title=title[:200],
        summary=f"{decision}: {item.goal[:200]}",
        created_at=when,
        updated_at=when,
        importance=4,
        tags=[_SCOUT_TAG, decision],
        stability=Stability.ACTIVE,
        source_type=_SCOUT_TAG,
    )
    try:
        ok = store.write(fm, body, subdir_override=_SCOUT_FEEDBACK_SUBDIR)
    except Exception:  # noqa: BLE001
        log.exception("scout: feedback memory write raised")
        return None
    if not ok:
        log.info("scout: feedback memory write declined by store")
        return None
    return mem_id


# ── Query gen + evaluation prompts ─────────────────────────────────────


def _build_query_prompt(
    *, identity: str, agenda: list[str], memory: list[str], history: list[str],
    topics: list[str], max_queries: int,
) -> str:
    parts: list[str] = [
        "You are TARS, deriving web-search queries for autonomous discovery.",
        "",
        "Discovery is domain-unbounded: gaming, science, fashion, tech,",
        "models, medicine — anything that could genuinely matter to TARS or",
        "the operator. Draw on who TARS is, what TARS is working on right",
        "now, what has landed well or poorly before, to decide where",
        "curiosity should point today.",
        "",
        f"Return AT MOST {max_queries} search queries as a JSON object, no",
        "preamble, no code fence:",
        '{"queries": ["<query 1>", "<query 2>", ...]}',
        "",
        "--- IDENTITY ---",
        identity or "(none)",
        "",
        "--- OPEN AGENDA ---",
    ]
    parts.extend(agenda or ["(none)"])
    parts.extend(["", "--- RECENT MEMORY ---"])
    parts.extend(memory or ["(none)"])
    parts.extend(["", "--- PAST SCOUT DECISIONS (accept/reject) ---"])
    parts.extend(history or ["(none)"])
    if topics:
        parts.extend([
            "",
            "--- SEED TOPIC HINTS (hints only, NOT an allowlist — you may",
            "search well outside these) ---",
        ])
        parts.extend(f"- {t}" for t in topics)
    parts.extend(["", "Return the JSON object now."])
    return "\n".join(parts)


def _build_eval_prompt(fresh: list[dict[str, Any]], *, identity: str, max_proposals: int) -> str:
    parts: list[str] = [
        "You are TARS, deciding which discoveries are worth OUR time.",
        "",
        f"Pick AT MOST {max_proposals} candidates below that a curious,",
        "resource-aware assistant would actually bring to the operator. For",
        "each pick, give an explicit \"why us / why now\" line — why this",
        "matters to TARS specifically and why surfacing it now, grounded",
        "only in the evidence given below. If a pick proposes swapping a",
        "model or provider, put the exact providers.yaml / roles.yaml diff",
        "as TEXT in diff_text — never apply it yourself.",
        "",
        "RESPONSE FORMAT (JSON object, no preamble, no code fence):",
        '{"picks": [',
        "  {",
        '    "candidate_index": <int index from the list below>,',
        '    "why_us_why_now": "<one or two sentences>",',
        '    "diff_text": "<optional providers.yaml/roles.yaml diff, else \\"\\">"',
        "  }",
        "]}",
        "",
        "If nothing clears the bar, return an empty picks array.",
        "",
        "--- IDENTITY ---",
        identity or "(none)",
        "",
        "--- CANDIDATES ---",
    ]
    for i, c in enumerate(fresh):
        parts.append(f"[{i}] {c['title']} | {c['url']}")
        if c.get("content"):
            parts.append(f"    {c['content'][:300]}")
    parts.extend(["", "Return the JSON object now."])
    return "\n".join(parts)


async def _call_chain(prompt: str, chain: list, timeout_s: float) -> str:
    from tesseract.kernel.adapters.base import AdapterOptions

    for adapter, options in chain:
        try:
            out = await asyncio.wait_for(
                adapter.generate(prompt, options or AdapterOptions()), timeout=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("scout: LLM call failed (%s)", exc)
            continue
        if out and out.strip():
            return out
    return ""


def _parse_envelope(raw: str, model_cls: type[BaseModel]) -> Any:
    if not raw or not raw.strip():
        return model_cls()
    match = _JSON_OBJECT_RE.search(raw)
    if match is None:
        log.warning("scout: no JSON object in adapter output")
        return model_cls()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("scout: JSON parse failed")
        return model_cls()
    if not isinstance(data, dict):
        return model_cls()
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        log.warning("scout: response failed validation: %s", exc)
        return model_cls()


async def _evaluate(
    chain: list, fresh: list[dict[str, Any]], *, identity: str, max_proposals: int,
) -> list[_EvalPick]:
    prompt = _build_eval_prompt(fresh, identity=identity, max_proposals=max_proposals)
    raw = await _call_chain(prompt, chain, _LLM_TIMEOUT_S)
    picks = _parse_envelope(raw, _EvalResponse).picks
    return picks[:max_proposals]


# ── Sweep (search + feeds, per-source breaker) ─────────────────────────


def _make_search_fetcher():
    """Direct Tavily search call — injectable for tests. Raises on any
    failure so ``_run_source`` can feed the per-source breaker and the
    caller's ``asyncio.gather(..., return_exceptions=True)`` isolates a
    dead source."""
    import httpx

    endpoint = "https://api.tavily.com/search"
    timeout_s = 15.0

    async def _fetch(query: str) -> list[dict[str, Any]]:
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY not set")
        payload = {
            "query": query, "max_results": 5, "search_depth": "basic",
            "topic": "general", "include_answer": False,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(endpoint, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        return [
            {
                "title": str(h.get("title") or ""),
                "url": str(h.get("url") or ""),
                "content": str(h.get("content") or ""),
            }
            for h in results if isinstance(h, dict)
        ]

    return _fetch


def _make_feed_fetcher():
    """Plain HTTP GET of a feed URL, split generically into title+link
    entries via anchor tags — no per-feed parser. Raises on any failure."""
    import httpx

    timeout_s = 20.0

    async def _fetch(feed_url: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            r = await client.get(feed_url)
        r.raise_for_status()
        out: list[dict[str, Any]] = []
        for match in _ANCHOR_RE.finditer(r.text):
            href = match.group(1).strip()
            title = " ".join(_TAG_RE.sub(" ", match.group(2)).split())
            if not href or not title:
                continue
            url = href if href.startswith("http") else urljoin(feed_url, href)
            out.append({"title": title, "url": url, "content": ""})
            if len(out) >= _MAX_FEED_ENTRIES:
                break
        return out

    return _fetch


def _breaker_log_dir() -> Path:
    """Call-time resolution (never an import-time constant) so a test's
    ``monkeypatch.setenv("TESSERACT_HOME", tmp_path)`` lands the breaker
    JSONL under its own tmp dir — mirrors ``spawn_wake._wake_breaker_log_dir``."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "logs" / "circuit-breakers"


def _domain_key(url: str) -> str:
    domain = (urlsplit(url).netloc or url).replace(":", "_")
    return f"scout_{domain}"


async def _run_source(fetch, arg: str, breaker: CircuitBreaker) -> list[dict[str, Any]]:
    try:
        result = await fetch(arg)
    except Exception as exc:  # noqa: BLE001
        breaker.record_failure(str(exc))
        raise
    breaker.record_success()
    return result


async def _sweep(
    queries: list[str], feed_urls: list[str],
) -> tuple[list[dict[str, Any]], list[str], bool, int]:
    search_fetch = _make_search_fetcher()
    feed_fetch = _make_feed_fetcher()
    log_dir = _breaker_log_dir()

    tasks: list[Any] = []
    task_keys: list[str] = []
    notes: list[str] = []
    attempted = 0

    tavily_breaker = CircuitBreaker(name="scout_tavily", log_dir=log_dir)
    for q in queries:
        if tavily_breaker.is_tripped:
            notes.append("scout_tavily: breaker open, skipped")
            continue
        attempted += 1
        tasks.append(_run_source(search_fetch, q, tavily_breaker))
        task_keys.append("scout_tavily")

    for url in feed_urls:
        key = _domain_key(url)
        breaker = CircuitBreaker(name=key, log_dir=log_dir)
        if breaker.is_tripped:
            notes.append(f"{key}: breaker open, skipped")
            continue
        attempted += 1
        tasks.append(_run_source(feed_fetch, url, breaker))
        task_keys.append(key)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    candidates: list[dict[str, Any]] = []
    failed = 0
    for key, res in zip(task_keys, results):
        if isinstance(res, BaseException):
            failed += 1
            notes.append(f"{key}: {res}")
            continue
        candidates.extend(res or [])

    all_failed = attempted > 0 and failed == attempted
    return candidates, notes, all_failed, attempted


# ── Dedup / pre-filter / publish ────────────────────────────────────────


def _normalize_key(value: str) -> str:
    v = value.strip()
    if not v:
        return ""
    parsed = urlsplit(v)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
        if parsed.query:
            normalized += f"?{parsed.query}"
        return normalized
    return v.lower()


def _dedup_and_prefilter(candidates: list[dict[str, Any]], seen: set[str]) -> list[dict[str, Any]]:
    fresh: list[dict[str, Any]] = []
    seen_this_run: set[str] = set()
    for c in candidates:
        title = str(c.get("title") or "").strip()
        url = str(c.get("url") or "").strip()
        if not title or not url:
            continue
        key = _normalize_key(url)
        if not key or key in seen or key in seen_this_run:
            continue
        seen_this_run.add(key)
        fresh.append({"title": title, "url": url, "content": str(c.get("content") or ""), "_key": key})
        if len(fresh) >= _MAX_CANDIDATES_FOR_EVAL:
            break
    return fresh


def _publish_picks(picks: list[_EvalPick], fresh: list[dict[str, Any]], *, when: datetime) -> list[str]:
    event_ids: list[str] = []
    for pick in picks:
        if pick.candidate_index < 0 or pick.candidate_index >= len(fresh):
            continue
        candidate = fresh[pick.candidate_index]
        payload = {
            "title": candidate["title"],
            "url": candidate["url"],
            "why_us_why_now": pick.why_us_why_now,
            "diff_text": pick.diff_text,
            "emitted_at": when.isoformat(),
            "source_handler": "scout",
        }
        digest = hashlib.sha1(candidate["_key"].encode("utf-8")).hexdigest()[:16]
        event_id = f"evt_scout_{digest}"
        publish_to_bus(AgendaSource.SCOUT, payload, event_id=event_id)
        event_ids.append(event_id)
    return event_ids


# ── Persistent ledgers (seen-store, feedback-fed ids, horizon) ─────────


def _autonomy_dir() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "autonomy"


def _resolve_seen_path(ctx: JobContext) -> Path:
    override = ctx.config.get("seen_path")
    return Path(override) if override else _autonomy_dir() / "scout-seen.jsonl"


def _resolve_fedback_path(ctx: JobContext) -> Path:
    override = ctx.config.get("fedback_path")
    return Path(override) if override else _autonomy_dir() / "scout-feedback-seen.jsonl"


def _resolve_horizon_path(ctx: JobContext) -> Path:
    override = ctx.config.get("horizon_path")
    return Path(override) if override else _autonomy_dir() / "scout-horizon.jsonl"


def _read_seen(path: Path) -> set[str]:
    """Permanent membership — unlike the heartbeat's rolling-window seen
    ledger, a scout URL never expires back into circulation on its own
    (staleness only governs unacted agenda proposals, via the reaper)."""
    if not path.exists():
        return set()
    keys: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = str(row.get("key") or "")
                if key:
                    keys.add(key)
    except OSError:
        log.exception("scout: seen read failed")
        return set()
    return keys


def _update_seen(path: Path, keys: list[str], *, when: datetime) -> None:
    if not keys:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for key in keys:
                fh.write(json.dumps({"key": key, "ts": when.isoformat()}) + "\n")
    except OSError:
        log.exception("scout: seen append failed")


def _read_id_ledger(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item_id = str(row.get("item_id") or "")
                if item_id:
                    ids.add(item_id)
    except OSError:
        log.exception("scout: id ledger read failed")
        return set()
    return ids


def _append_id_ledger(path: Path, *, item_id: str, when: datetime) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"item_id": item_id, "ts": when.isoformat()}) + "\n")
    except OSError:
        log.exception("scout: id ledger append failed")


def _record_horizon(path: Path, *, event_ids: list[str], staleness_days: int, when: datetime) -> None:
    """One row per published event — ``ScoutReaperJob`` joins on
    ``event_id`` (== the resulting item's ``source_event_id``) to recover
    the staleness horizon that was in effect when it was proposed."""
    if not event_ids:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for event_id in event_ids:
                fh.write(json.dumps({
                    "event_id": event_id, "staleness_days": staleness_days, "ts": when.isoformat(),
                }) + "\n")
    except OSError:
        log.exception("scout: horizon ledger append failed")


def _result(ctx: JobContext, t0: float, *, ok: bool, detail: str) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=ok,
        detail=detail,
        payload={},
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


__all__ = ["ScoutJob"]
