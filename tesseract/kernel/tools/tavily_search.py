"""TavilySearchTool — web search via Tavily API.

Returns LLM-optimized snippets: title, URL, content (pre-extracted),
score, and optionally a synthesized answer. Use for research-style
questions where you need digestible per-result content to answer.

Contrast with `web_search` (Brave): Brave gives you breadth + short
descriptions, better for "what's out there." Tavily gives you denser
per-result content, better for "answer my question from the web."

Requires `TAVILY_API_KEY` in .env. 1K req/mo free at tavily.com.
"""

from __future__ import annotations

import logging
import os
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.web_providers import fetch_json
from tesseract.kernel.tools.web_providers.tavily import TavilySearchProvider

_TIMEOUT = 15.0
_MAX_RESULTS = 10

_PROVIDER = TavilySearchProvider()


class TavilySearchInput(BaseModel):
    query: str = Field(description="Search query")
    max_results: int = Field(default=5, ge=1, le=_MAX_RESULTS, description="Number of results (1-10)")
    search_depth: str = Field(default="basic", description="'basic' (fast, cheaper) or 'advanced' (deeper, slower)")
    topic: str = Field(default="general", description="'general' or 'news'")
    include_answer: bool = Field(default=True, description="Include a synthesized answer at the top of results")
    include_domains: list[str] = Field(default_factory=list, description="Restrict to these domains")
    exclude_domains: list[str] = Field(default_factory=list, description="Exclude these domains")


class TavilySearchTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"
    @property
    def name(self) -> str:
        return "tavily_search"

    @property
    def description(self) -> str:
        return (
            "Search the web via Tavily — LLM-optimized snippets with optional synthesized answer. "
            "Use for research questions where you need digestible per-result content to answer. "
            "Use web_search (Brave) for breadth / news / niche queries instead."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return TavilySearchInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, TavilySearchInput) else TavilySearchInput(**tool_input.model_dump())

        api_key = os.environ.get(_PROVIDER.api_key_env)
        if not api_key:
            return ToolResult(output=_PROVIDER.missing_key_message(), is_error=True)

        if inp.search_depth not in ("basic", "advanced"):
            return ToolResult(output=f"search_depth must be 'basic' or 'advanced', got {inp.search_depth!r}", is_error=True)
        if inp.topic not in ("general", "news"):
            return ToolResult(output=f"topic must be 'general' or 'news', got {inp.topic!r}", is_error=True)

        outcome = await fetch_json(
            _PROVIDER,
            api_key=api_key,
            request=_PROVIDER.build_request(inp),
            timeout=_TIMEOUT,
            note_tripwire=_note_tavily_tripwire,
        )
        if outcome.error is not None:
            return outcome.error

        results, answer = _PROVIDER.parse_results(outcome.data or {})
        if not results and not answer:
            return ToolResult(output=f"No results for '{inp.query}'")

        lines: list[str] = [f"Tavily: {inp.query}  ({len(results)} results)"]
        if inp.include_answer and answer:
            lines.append(f"\nAnswer: {answer}")
        for i, hit in enumerate(results, 1):
            title = (hit.get("title") or "").strip()
            url = (hit.get("url") or "").strip()
            content = (hit.get("content") or "").strip().replace("\n", " ")
            score = hit.get("score")
            score_str = f" · score={score:.2f}" if isinstance(score, (int, float)) else ""
            lines.append(f"\n[{i}] {title}{score_str}\n    {url}\n    {content}")

        return ToolResult(
            output="\n".join(lines),
            metadata={"count": len(results), "query": inp.query, "depth": inp.search_depth},
        )


def _note_tavily_tripwire(drift_kind: str, evidence: dict) -> None:
    """AU-14 14b production tripwire — best-effort JSONL row write."""
    try:
        from tesseract.orchestrator.provider_health import note_production_tripwire
        note_production_tripwire("tavily_search", _PROVIDER.tripwire_source, drift_kind, evidence)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug(
            "tavily_search: tripwire write failed", exc_info=True
        )
