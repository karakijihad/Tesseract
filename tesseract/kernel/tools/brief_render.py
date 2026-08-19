"""brief_render — operator-facing tool that renders today's daily brief.

Wraps :class:`tesseract.orchestrator.brief.renderer.BriefRenderer`. The
``/brief`` REPL alias resolves to this tool. Synchronous overwrite is
the contract — a re-run on the same day replaces today's file (matches
the `/brief` slash semantics in `_shared/brief-renderer-spec.md`). The
the nightly `brief_render` stage calls the renderer directly with
``overwrite=False`` so a missed slot does not double-write.

ASK-gated. The renderer fires Tavily searches and writes to
``memory-store/daily/briefs/``; both side-effects warrant an operator
prompt even though the tool itself is operator-initiated.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract import http_client
from tesseract.agents.loader import load_agent
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.memory.store import MemoryStore
from tesseract.orchestrator.brief.pillars import DEFAULT_PILLARS, Pillar
from tesseract.orchestrator.brief.renderer import BriefRenderer, CostCaps
from tesseract.paths import TESSERACT_HOME

logger = logging.getLogger(__name__)

DEFAULT_DIGESTER_TIMEOUT_S = 60.0


class BriefRenderInput(BaseModel):
    date: str = Field(
        default="",
        description=(
            "Target ISO date (YYYY-MM-DD). Empty = today UTC. The renderer "
            "writes to ``memory-store/daily/briefs/<iso-date>.md``."
        ),
    )
    overwrite: bool = Field(
        default=True,
        description=(
            "When true (the /brief default), replace today's file. When "
            "false (cron default) and the file already exists, the call "
            "returns the existing brief without re-running the digesters."
        ),
    )


class BriefRenderTool(Tool):
    default_posture: ClassVar[str] = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = "Render today's daily brief by running the digester sub-agents."
    use_when: ClassVar[str] = (
        "Use when the operator asks to build or refresh the daily brief. "
        "Writes to memory-store/daily/briefs/ and searches the web."
    )
    not_when: ClassVar[str] = (
        "to read a brief that already exists, use `brief_read` instead — it "
        "has no side effects."
    )

    def __init__(
        self,
        *,
        adapter: ModelAdapter | None = None,
        adapter_options: AdapterOptions | None = None,
        memory_store: MemoryStore | None = None,
        cost_caps: CostCaps | None = None,
        agents_dir: Path | None = None,
        briefs_dir: Path | None = None,
        pillars: tuple[Pillar, ...] = DEFAULT_PILLARS,
        interests_path: Path | None = None,
        event_store: "object | None" = None,
        vault_wiki_dir: Path | None = None,
        vault_raw_dir: Path | None = None,
        vault_librarian: "object | None" = None,
    ) -> None:
        # Late-bind TESSERACT_HOME at constructor call time so a process
        # that toggles the env var post-import (test harness, alt-home
        # boot) still routes writes to the operator-chosen home. The
        # pre-MO-9-13 code captured the import-time constant; the
        # MO-9-13 reviewer flagged that as an IMPORTANT inconsistency
        # with daily_brief._resolve_briefs_dir / _resolve_interests_path,
        # which already late-bind. Mirror that pattern here.
        home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
        self._adapter = adapter
        self._adapter_options = adapter_options or AdapterOptions()
        self._memory_store = memory_store
        self._cost_caps = cost_caps or CostCaps()
        # `None` means the live pair of agent roots (AR-6): the digester
        # cards are shipped, so they resolve out of the app tree unless the
        # operator shadows one. Naming a directory here restricts the load
        # to it, which is what the tests want and production does not.
        self._agents_dir = agents_dir
        self._briefs_dir = briefs_dir or (home / "memory-store" / "daily" / "briefs")
        self._pillars = pillars
        self._interests_path = interests_path or (
            home / "memory-store" / "interests" / "profile.yaml"
        )
        self._event_store = event_store
        self._vault_wiki_dir = vault_wiki_dir or (home / "vault" / "wiki")
        self._vault_raw_dir = vault_raw_dir or (home / "vault" / "raw")
        self._vault_librarian = vault_librarian
        self._ecosystem_home = home

    @property
    def name(self) -> str:
        return "brief_render"

    @property
    def input_schema(self) -> type[BaseModel]:
        return BriefRenderInput

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: BriefRenderInput = tool_input  # type: ignore[assignment]
        target = _parse_target_date(inp.date)
        if target is None:
            return ToolResult(
                output=f"invalid date {inp.date!r}: expected YYYY-MM-DD",
                is_error=True,
            )

        compile_fn = getattr(self._vault_librarian, "compile_source", None) if self._vault_librarian else None
        renderer = BriefRenderer(
            briefs_dir=self._briefs_dir,
            pillars=self._pillars,
            interests_path=self._interests_path,
            invoke_digester=_make_digester_invoker(
                self._adapter, self._adapter_options, self._agents_dir,
            ),
            tavily_search=_make_tavily_fetcher(context),
            memory_store=self._memory_store,
            cost_caps=self._cost_caps,
            event_store=self._event_store,
            vault_wiki_dir=self._vault_wiki_dir,
            vault_raw_dir=self._vault_raw_dir,
            librarian_compile=compile_fn,
            ecosystem_home=self._ecosystem_home,
        )
        try:
            result = await renderer.render(target, overwrite=inp.overwrite)
        except Exception as exc:  # noqa: BLE001
            logger.exception("brief_render failed")
            return ToolResult(output=f"brief_render failed: {exc!r}", is_error=True)

        if result.skipped_existing:
            return ToolResult(
                output=(
                    f"brief for {target.isoformat()} already exists at "
                    f"{result.path}; pass overwrite=true to replace."
                ),
                metadata={"path": str(result.path), "skipped_existing": True},
            )

        return ToolResult(
            output=(
                f"brief rendered for {target.isoformat()} → {result.path} "
                f"(sections: {', '.join(result.sections_rendered) or 'all empty'}; "
                f"tavily_calls={result.tavily_calls}; cost_cap_hit={result.cost_cap_hit})"
            ),
            metadata={
                "path": str(result.path),
                "sections_rendered": result.sections_rendered,
                "sections_dropped": result.sections_dropped,
                "tavily_calls": result.tavily_calls,
                "estimated_usd": result.estimated_usd,
                "cost_cap_hit": result.cost_cap_hit,
                "memory_id": result.memory_id,
                "workspace_event_id": result.workspace_event_id,
            },
        )


def _parse_target_date(raw: str) -> date | None:
    stripped = raw.strip()
    if not stripped:
        return datetime.now(timezone.utc).date()
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        return None


def _make_digester_invoker(
    adapter: ModelAdapter | None,
    options: AdapterOptions,
    agents_dir: Path | None,
):
    """Return an ``invoke_digester(name, payload)`` coroutine that loads
    the agent's Role/Inputs/Rules sections as a system prompt and calls
    the configured adapter. Mirrors :mod:`provider_watch`'s direct-adapter
    pattern so the renderer works in scheduler contexts where
    ``invoke_agent`` is not wired.
    """
    async def _invoke(name: str, payload: dict) -> str:
        if adapter is None:
            return ""
        system_prompt = _load_agent_system_prompt(name, agents_dir)
        if not system_prompt:
            return ""
        body = _format_payload_for_prompt(payload)
        prompt = f"{system_prompt}\n\n---\n\nPayload:\n{body}\n\n---\n\nProduce your section now. Markdown body only, no preamble."
        try:
            text = await adapter.generate(prompt, options)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brief: adapter call for %s failed (%s)", name, exc)
            return ""
        return (text or "").strip()

    return _invoke


def _make_tavily_fetcher(_context: ToolContext):
    """Return a Tavily fetcher that hits the API directly.

    ``TavilySearchTool`` collapses results into prose for the chat surface;
    the renderer needs per-hit ``url`` for the dedupe store, so we call
    the same endpoint with the same auth and return the structured
    ``results`` list. Any failure (missing key, timeout, non-200) yields
    ``[]`` and the renderer logs + continues to the next topic.
    """
    import os

    import httpx

    endpoint = "https://api.tavily.com/search"
    timeout_s = 15.0

    async def _fetch(query: str, options: dict) -> list[dict]:
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            logger.info("brief: TAVILY_API_KEY not set; skipping query %r", query)
            return []
        payload: dict[str, object] = {
            "query": query,
            "max_results": int(options.get("max_results", 5)),
            "search_depth": "basic",
            # Caller may override the index: the daily brief wants "news",
            # job-posting sweeps want "general". Default preserves brief behavior.
            "topic": options.get("topic", "news"),
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
            logger.info("brief: tavily query %r failed (%s)", query, exc)
            return []
        if r.status_code != 200:
            logger.info("brief: tavily %s for query %r", r.status_code, query)
            return []
        try:
            data = r.json()
        except ValueError:
            return []
        results = data.get("results") or []
        return [hit for hit in results if isinstance(hit, dict)]

    return _fetch


def _load_agent_system_prompt(name: str, agents_dir: Path | None) -> str:
    try:
        agent = load_agent(name, agents_dir=agents_dir)
    except FileNotFoundError:
        logger.warning(
            "brief: agent %r not found under %s", name,
            agents_dir or "the shipped and operator agent roots",
        )
        return ""
    sections = ["Role", "Inputs", "Output structure", "Rules", "Anti-output"]
    parts: list[str] = []
    for section in sections:
        body = agent.get_section(section)
        if body:
            parts.append(f"## {section}\n{body}")
    return "\n\n".join(parts)


def _format_payload_for_prompt(payload: dict) -> str:
    import json
    try:
        return json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(payload)


__all__ = ["BriefRenderTool", "BriefRenderInput"]
