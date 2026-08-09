"""ProviderWatchJob — daily LLM-provider release watcher.

For each tracked provider, runs a Tavily news-topic search for recent
model / pricing / context-window changes, aggregates the results into a
brief, and asks the `provider-watcher` agent (via direct adapter call)
to render a tight markdown digest. Output lands at
``memory-store/daily/providers/YYYY-MM-DD.md`` and is idempotent — a
second run on the same day overwrites (operator-driven manual refresh
should always produce fresh content).

The job is a thin orchestrator: Tavily is called from Python (the
adapter doesn't get tool access from a scheduler context), and the
agent's `Role` section is loaded directly as the system prompt. When
PTY-backed delegate or `invoke_agent`-from-scheduler lands, this can
move to the full agent runtime; for now the direct path keeps the
moving parts small.

Disabled by default in ``schedule.yaml``. Operator flips it on once the
TAVILY_API_KEY is set and the digest content matches their taste.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.agents.loader import load_agent
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.tavily_search import TavilySearchInput, TavilySearchTool
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.brain.cost.metered_adapter import meter_chain
from tesseract.scheduler.role_chain import build_chain_for_role, resolve_role_name
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_RESULTS_PER_PROVIDER = 5

# Default tracked roster. Operator overrides via
# `schedule.yaml::jobs.provider_watch.config.providers` — each entry is
# {name, queries: [...]}. The default queries are tuned for "what just
# shipped / changed" rather than "what is X." If a provider's docs site
# is reputable, restricting to its domain (config-level
# `include_domains`) tightens noise further.
_DEFAULT_PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "Anthropic",
        "queries": [
            "Anthropic Claude new model release",
            "Anthropic Claude pricing context window changelog",
            "Claude Code CLI release notes new slash command",
        ],
    },
    {
        "name": "OpenAI",
        "queries": [
            "OpenAI GPT new model release",
            "OpenAI Codex CLI changelog new feature",
            "OpenAI API pricing context window changes",
        ],
    },
    {
        "name": "Google",
        "queries": [
            "Google Gemini new model release",
            "Google AI Studio pricing context window",
        ],
    },
    {
        "name": "NVIDIA NIM",
        "queries": [
            "NVIDIA NIM new model deployment",
            "NIM endpoint pricing changes",
        ],
    },
    {
        "name": "ElevenLabs",
        "queries": [
            "ElevenLabs new voice model release",
            "ElevenLabs pricing changes",
        ],
    },
    {
        "name": "Ecosystem",
        "queries": [
            "MCP server new release Model Context Protocol",
            "agent framework release LLM tools 2026",
        ],
    },
]


class ProviderWatchJob(BaseJob):
    uses_llm = True
    default_model_role = "agents_default"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            # Differentiate absent (use defaults) from explicit empty
            # (operator asked for nothing — that's a misconfig). The naive
            # `get(...) or default` collapsed those two cases.
            if "providers" in ctx.config:
                providers = ctx.config["providers"]
            else:
                providers = _DEFAULT_PROVIDERS
            if not isinstance(providers, list) or not providers:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="providers config empty",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            target_date = ctx.fired_at.astimezone(timezone.utc).date()
            max_results = int(
                ctx.config.get("max_results_per_provider", DEFAULT_MAX_RESULTS_PER_PROVIDER)
            )

            search_ctx = ToolContext(
                workspace_root=str(_resolve_workspace_root(ctx)),
                session_id=f"provider-watch-{target_date.isoformat()}",
                current_call_id=f"provider-watch-{ctx.run_id}",
            )
            brief = await _build_brief(
                providers=providers,
                target_date=target_date,
                max_results=max_results,
                search_ctx=search_ctx,
            )
            if not brief.strip():
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail="search returned no usable results",
                    payload={"target_date": target_date.isoformat(), "wrote": False},
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            chain = meter_chain(_resolve_adapter_chain(ctx), ctx.cost_ledger)
            if not chain:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail="adapter unavailable — skipped digest",
                    payload={"target_date": target_date.isoformat(), "wrote": False},
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            system_prompt = _load_agent_role_section()
            digest = await _generate_digest_with_fallback(
                system_prompt=system_prompt,
                brief=brief,
                chain=chain,
                timeout_s=DEFAULT_TIMEOUT_S,
            )
            if not digest.strip():
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="chain exhausted — no digest produced",
                    payload={"target_date": target_date.isoformat(), "wrote": False},
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            out_path = _digest_path(ctx, target_date)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(digest.strip() + "\n", encoding="utf-8")

            kb_summary = await _write_provider_kb(
                providers=providers,
                target_date=target_date,
                search_ctx=search_ctx,
                max_results=max_results,
                tavily_call_cap=int(ctx.config.get("kb_max_tavily_calls", 15))
                if isinstance(ctx.config, dict)
                else 15,
            )

            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"wrote {out_path.name}",
                payload={
                    "target_date": target_date.isoformat(),
                    "wrote": True,
                    "path": str(out_path),
                    "digest_chars": len(digest.strip()),
                    "providers": [p.get("name") for p in providers],
                    "kb": kb_summary,
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("provider_watch crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_workspace_root(ctx: JobContext) -> Path:
    """The root relative tool paths resolve against: the operator's home.

    `home_dir()`, not the parent of the code tree. Under the three-sibling
    layout `TESSERACT_HOME.parent` is the INSTALL root — the directory that
    also holds the sealed `app/` — so a relative path from a tool running
    here could resolve into code. On a dev checkout the two happen to
    coincide, which is why this went unnoticed.

    It also matches what actually adjudicates those paths:
    `load_permission_policy` is built with `workspace_root=str(home_dir())`
    (`mirror/server/config.py`, `scripts/agent_controller.py`). A context
    rooted anywhere else disagrees with the policy deciding its writes.

    Resolved per call rather than captured at import, like its `paths.py`
    siblings, so a relocated home is honoured without a restart.
    """
    from tesseract.paths import home_dir

    return home_dir()


def _digest_path(ctx: JobContext, target_date) -> Path:
    override = ctx.config.get("digest_dir")
    if override:
        return Path(override) / f"{target_date.isoformat()}.md"
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        tdir = app.get("tesseract_dir")
        if tdir is not None:
            return Path(tdir) / "memory-store" / "daily" / "providers" / f"{target_date.isoformat()}.md"
    return TESSERACT_HOME / "memory-store" / "daily" / "providers" / f"{target_date.isoformat()}.md"


def _load_agent_role_section() -> str:
    """Load the `provider-watcher` agent's Role section as system prompt.

    Decoupling the prompt from the job code so the operator can iterate
    on tone/format in the .md file without touching Python. If the file
    is missing we fall back to a baked-in baseline so the job still
    produces output.
    """
    try:
        agent = load_agent("provider-watcher")
    except FileNotFoundError:
        log.warning("provider_watch: provider-watcher agent missing; using baked baseline")
        return (
            "You are the assistant's provider/model watcher. Produce a tight markdown "
            "digest of new models, pricing changes, context-window changes, "
            "and deprecations from the brief below. One bullet per item, "
            "URL at the end. Skip providers with no new info. No preamble."
        )
    role = agent.get_section("Role")
    structure = agent.get_section("Output structure")
    rules = agent.get_section("Rules")
    anti = agent.get_section("Anti-output")
    parts = [role]
    if structure:
        parts.append("## Output structure\n" + structure)
    if rules:
        parts.append("## Rules\n" + rules)
    if anti:
        parts.append("## Anti-output\n" + anti)
    return "\n\n".join(p for p in parts if p)


async def _build_brief(
    *,
    providers: list[dict[str, Any]],
    target_date,
    max_results: int,
    search_ctx: ToolContext,
) -> str:
    """Run Tavily for each provider × query and stitch results into a brief.

    Per-query failures are logged at INFO; we don't fail the whole job
    if Tavily is down for a single provider. The agent handles empty
    sections by omitting them.
    """
    tool = TavilySearchTool()
    lines: list[str] = [
        f"Today is {target_date.isoformat()}.",
        f"Cutoff: include only items dated on or after {target_date.isoformat()} minus 7 days.",
        "",
        "## Search results per provider",
        "",
    ]
    found_any = False
    for provider in providers:
        name = str(provider.get("name") or "").strip()
        queries = provider.get("queries") or []
        if not name or not isinstance(queries, list) or not queries:
            continue
        section_rows: list[str] = []
        for query in queries:
            q = str(query).strip()
            if not q:
                continue
            try:
                result = await tool.run(
                    TavilySearchInput(
                        query=q,
                        max_results=max_results,
                        search_depth="basic",
                        topic="news",
                        include_answer=False,
                    ),
                    search_ctx,
                )
            except Exception as exc:  # noqa: BLE001
                log.info("provider_watch: tavily failed for %s/%s (%s)", name, q, exc)
                continue
            if result.is_error or not result.output.strip():
                continue
            section_rows.append(f"### Query: {q}\n{result.output.strip()}\n")
        if section_rows:
            found_any = True
            lines.append(f"### {name}")
            lines.extend(section_rows)
            lines.append("")
    if not found_any:
        return ""
    return "\n".join(lines)


async def _generate_digest_with_fallback(
    *,
    system_prompt: str,
    brief: str,
    chain: list[tuple[ModelAdapter, AdapterOptions]],
    timeout_s: float,
) -> str:
    prompt = (
        f"{system_prompt}\n\n"
        f"---\n\n"
        f"{brief}\n\n"
        f"---\n\n"
        f"Render the digest now. Markdown only, no preamble."
    )
    for index, (adapter, options) in enumerate(chain):
        label = f"{getattr(options, 'provider', None) or 'unknown'}/{getattr(options, 'model', None) or '?'}"
        try:
            digest = await asyncio.wait_for(
                adapter.generate(prompt, options or AdapterOptions()),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("provider_watch: %s timed out after %.1fs (chain idx=%d)", label, timeout_s, index)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("provider_watch: %s call failed (%s) (chain idx=%d)", label, exc, index)
            continue
        if digest and digest.strip():
            if index > 0:
                log.info("provider_watch: fell back to chain idx=%d (%s)", index, label)
            return digest
        log.warning("provider_watch: %s returned empty digest (chain idx=%d)", label, index)
    return ""


_PROVIDER_KB_SLUG = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "nvidia nim": "nvidia-nim",
    "nvidia": "nvidia-nim",
    "elevenlabs": "elevenlabs",
    "meta": "meta",
}


async def _write_provider_kb(
    *,
    providers: list[dict[str, Any]],
    target_date,
    search_ctx: ToolContext,
    max_results: int,
    tavily_call_cap: int,
) -> dict[str, Any]:
    """MO-10-1 §2b extension. After the digest lands, write one
    ``vault/knowledge-base/providers/<provider>.md`` per tracked provider
    through the content-merge protocol so operator hand-edits survive.

    The structured ``canonical_models`` frontmatter starts empty in v1 —
    Tavily search snippets aren't reliably parseable into model entries
    without an LLM round-trip, and v1 prefers an honest empty list over
    half-baked structured output. Operators (or a future agent step) can
    populate it; MO-10-2's emit path treats empty as "no proposal".

    Returns a summary dict for the JobResult payload.
    """
    from tesseract.knowledge_keeper import (
        MergeConflict,
        append_refresh_row,
        ensure_kb_tree,
        merge_kb_file,
    )
    from tesseract.knowledge_keeper.scaffolding import regenerate_index

    kb_base = ensure_kb_tree()
    tool = TavilySearchTool()
    refreshed: list[str] = []
    conflicts: list[str] = []
    calls = 0
    for entry in providers:
        if calls >= tavily_call_cap:
            log.info("provider_watch KB: tavily cap reached after %d calls", calls)
            break
        name = str(entry.get("name") or "").strip()
        slug = _PROVIDER_KB_SLUG.get(name.lower())
        if not slug:
            continue
        queries = entry.get("queries") or []
        if not isinstance(queries, list) or not queries:
            continue

        rows: list[tuple[str, str]] = []
        source_urls: list[str] = []
        for q in queries:
            if calls >= tavily_call_cap:
                break
            qs = str(q).strip()
            if not qs:
                continue
            calls += 1
            try:
                result = await tool.run(
                    TavilySearchInput(
                        query=qs,
                        max_results=max_results,
                        search_depth="basic",
                        topic="news",
                        include_answer=False,
                    ),
                    search_ctx,
                )
            except Exception as exc:  # noqa: BLE001
                log.info("provider_watch KB: tavily failed for %s/%s (%s)", slug, qs, exc)
                continue
            if result.is_error or not result.output.strip():
                continue
            rows.append((qs, result.output.strip()))
            source_urls.extend(_kb_extract_urls(result.output))

        target = kb_base / "providers" / f"{slug}.md"
        if not rows and not target.exists():
            continue
        fm = {
            "provider": slug,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source_urls": _kb_dedupe(source_urls)[:10],
            "canonical_models": [],
        }
        body = _kb_render_body(name, target_date, rows)
        outcome = merge_kb_file(
            target=target,
            new_frontmatter=fm,
            new_body=body,
        )
        if isinstance(outcome, MergeConflict):
            conflicts.append(outcome.file)
            append_refresh_row(
                kb_base / "providers",
                file=target.name,
                diff_summary="merge conflict — file left unchanged",
                extra={"conflict_sections": list(outcome.sections)},
            )
            continue
        append_refresh_row(
            kb_base / "providers",
            file=target.name,
            diff_summary=outcome.diff_summary,
        )
        if outcome.changed:
            refreshed.append(target.name)

    try:
        regenerate_index(kb_base)
    except Exception:  # noqa: BLE001
        log.exception("provider_watch KB: INDEX regen failed (non-fatal)")

    return {
        "refreshed": refreshed,
        "conflicts": conflicts,
        "tavily_calls": calls,
    }


def _kb_extract_urls(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        for token in line.split():
            t = token.strip().strip("()[],.;")
            if t.startswith("http://") or t.startswith("https://"):
                out.append(t)
    return out


def _kb_dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            deduped.append(it)
    return deduped


def _kb_render_body(name: str, target_date, rows: list[tuple[str, str]]) -> str:
    lines: list[str] = [f"# {name} — knowledge base", ""]
    lines.append(f"Last refreshed {target_date.isoformat()}.")
    lines.append("")
    lines.append("## Models")
    lines.append("")
    lines.append("(`canonical_models` frontmatter is the structured source. Empty in v1 — operator or agent step populates.)")
    lines.append("")
    lines.append("## Recent changes")
    lines.append("")
    if not rows:
        lines.append("(no fresh search results this cycle)")
        lines.append("")
        return "\n".join(lines)
    for q, output in rows:
        lines.append(f"### {q}")
        lines.append("")
        lines.append(output.strip())
        lines.append("")
    return "\n".join(lines)


def _resolve_adapter_chain(ctx: JobContext) -> list[tuple[ModelAdapter, AdapterOptions]]:
    role_name = resolve_role_name(ctx, ProviderWatchJob.default_model_role)
    app = ctx.app
    override_set = bool((ctx.model_role or "").strip())
    if override_set and role_name is not None:
        return build_chain_for_role(role_name, log_label="provider_watch")
    if app is not None and hasattr(app, "get"):
        live = app.get("adapter_chain") or []
        if live:
            return [(a, o or AdapterOptions()) for a, o in live if a is not None]
    if role_name is not None:
        built = build_chain_for_role(role_name, log_label="provider_watch")
        if built:
            return built
    if app is None or not hasattr(app, "get"):
        return []
    adapter = app.get("adapter")
    if adapter is None:
        return []
    return [(adapter, app.get("adapter_options") or AdapterOptions())]
