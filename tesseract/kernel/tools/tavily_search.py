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

import httpx
from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult

_ENDPOINT = "https://api.tavily.com/search"
_TIMEOUT = 15.0
_MAX_RESULTS = 10


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

        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            return ToolResult(
                output="TAVILY_API_KEY not set in .env. Get a free key (1K/mo) at https://tavily.com and add TAVILY_API_KEY=... to tesseract/.env",
                is_error=True,
            )

        if inp.search_depth not in ("basic", "advanced"):
            return ToolResult(output=f"search_depth must be 'basic' or 'advanced', got {inp.search_depth!r}", is_error=True)
        if inp.topic not in ("general", "news"):
            return ToolResult(output=f"topic must be 'general' or 'news', got {inp.topic!r}", is_error=True)

        payload: dict[str, object] = {
            "query": inp.query,
            "max_results": inp.max_results,
            "search_depth": inp.search_depth,
            "topic": inp.topic,
            "include_answer": inp.include_answer,
        }
        if inp.include_domains:
            payload["include_domains"] = inp.include_domains
        if inp.exclude_domains:
            payload["exclude_domains"] = inp.exclude_domains

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(_ENDPOINT, headers=headers, json=payload)
        except httpx.TimeoutException:
            _note_tavily_tripwire("latency_spike", {"timeout_seconds": _TIMEOUT})
            return ToolResult(output=f"Tavily search timed out after {_TIMEOUT}s", is_error=True)
        except httpx.HTTPError as e:
            _note_tavily_tripwire("http_error", {"exception": repr(e)})
            return ToolResult(output=f"Tavily search request failed: {e}", is_error=True)

        if r.status_code == 401:
            _note_tavily_tripwire("unavailable", {"status_code": 401})
            return ToolResult(output="Tavily 401 — API key rejected. Check TAVILY_API_KEY.", is_error=True)
        if r.status_code == 429:
            _note_tavily_tripwire("http_error", {"status_code": 429, "reason": "rate limit"})
            return ToolResult(output="Tavily 429 — rate limit exceeded (1K/mo on the free tier).", is_error=True)
        if r.status_code >= 400:
            _note_tavily_tripwire(
                "http_error",
                {"status_code": r.status_code, "body": r.text[:200]},
            )
            return ToolResult(output=f"Tavily {r.status_code}: {r.text[:200]}", is_error=True)

        try:
            data = r.json()
        except ValueError:
            _note_tavily_tripwire("shape_mismatch", {"reason": "non-JSON response"})
            return ToolResult(output="Tavily returned non-JSON", is_error=True)

        results = data.get("results") or []
        if not results and not data.get("answer"):
            return ToolResult(output=f"No results for '{inp.query}'")

        lines: list[str] = [f"Tavily: {inp.query}  ({len(results)} results)"]
        if inp.include_answer and data.get("answer"):
            lines.append(f"\nAnswer: {data['answer']}")
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
        note_production_tripwire("tavily_search", "api.tavily.search", drift_kind, evidence)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug(
            "tavily_search: tripwire write failed", exc_info=True
        )
