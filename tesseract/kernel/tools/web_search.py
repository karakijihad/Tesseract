"""WebSearchTool — web search via Brave Search API.

Returns ranked title / url / description triples for a query. ASK
permission — hitting an external service leaks the query to a third
party, so operator approval is required per invocation during testing.

Requires `BRAVE_SEARCH_API_KEY` in .env. Get a free key (2K req/mo) at
https://brave.com/search/api/. If no key is set, the tool errors
gracefully with a setup hint.
"""

from __future__ import annotations

import logging
import os
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_DEFAULT_TIMEOUT = 10.0
_MAX_RESULTS = 10


class WebSearchInput(BaseModel):
    query: str = Field(description="Search query")
    count: int = Field(default=5, ge=1, le=_MAX_RESULTS, description="Number of results to return (1-10)")
    country: str = Field(default="", description="2-letter country code for localized results, e.g. 'us', 'fr'. Empty = Brave's default.")


class WebSearchTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"
    # Audit-3 M9 — third-party search snippets are attacker-influenced
    # by definition (SEO-poisoned pages, ranked-result tampering). Wrap
    # in the UNTRUSTED_TOOL_OUTPUT envelope before history append.
    untrusted_source: ClassVar[bool] = True

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web via Brave Search. Returns a ranked list of title + URL + snippet. "
            "Use for current events, recent docs, or anything outside local memory/vault."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return WebSearchInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True  # no local mutation, but ASK anyway (see check_permissions)

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, WebSearchInput) else WebSearchInput(**tool_input.model_dump())

        api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
        if not api_key:
            return ToolResult(
                output="BRAVE_SEARCH_API_KEY not set in .env. Get a free key at https://brave.com/search/api/ and add BRAVE_SEARCH_API_KEY=... to tesseract/.env",
                is_error=True,
            )

        headers = {
            "X-Subscription-Token": api_key,
            "Accept": "application/json",
        }
        params: dict[str, str | int] = {"q": inp.query, "count": inp.count}
        if inp.country:
            params["country"] = inp.country

        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                r = await client.get(_BRAVE_ENDPOINT, headers=headers, params=params)
        except httpx.TimeoutException:
            _note_brave_tripwire("latency_spike", {"timeout_seconds": _DEFAULT_TIMEOUT})
            return ToolResult(
                output=f"Brave Search timed out after {_DEFAULT_TIMEOUT}s",
                is_error=True,
            )
        except httpx.HTTPError as e:
            _note_brave_tripwire("http_error", {"exception": repr(e)})
            return ToolResult(output=f"Brave Search request failed: {e}", is_error=True)

        if r.status_code == 401:
            _note_brave_tripwire("unavailable", {"status_code": 401})
            return ToolResult(output="Brave Search 401 — API key rejected. Check BRAVE_SEARCH_API_KEY.", is_error=True)
        if r.status_code == 429:
            _note_brave_tripwire("http_error", {"status_code": 429, "reason": "rate limit"})
            return ToolResult(output="Brave Search 429 — rate limit exceeded (2K/mo on the free tier).", is_error=True)
        if r.status_code >= 400:
            _note_brave_tripwire(
                "http_error",
                {"status_code": r.status_code, "body": r.text[:200]},
            )
            return ToolResult(output=f"Brave Search {r.status_code}: {r.text[:200]}", is_error=True)

        try:
            data = r.json()
        except ValueError:
            _note_brave_tripwire("shape_mismatch", {"reason": "non-JSON response"})
            return ToolResult(output="Brave Search returned non-JSON", is_error=True)

        results = (data.get("web") or {}).get("results") or []
        if not results:
            return ToolResult(output=f"No results for '{inp.query}'")

        lines: list[str] = [f"Search: {inp.query}  ({len(results)} results)"]
        for i, hit in enumerate(results[: inp.count], 1):
            title = hit.get("title", "").strip()
            url = hit.get("url", "").strip()
            desc = hit.get("description", "").strip().replace("\n", " ")
            lines.append(f"\n[{i}] {title}\n    {url}\n    {desc}")

        return ToolResult(
            output="\n".join(lines),
            metadata={"count": len(results), "query": inp.query},
        )


def _note_brave_tripwire(drift_kind: str, evidence: dict) -> None:
    """AU-14 14b production tripwire — best-effort JSONL row write."""
    try:
        from tesseract.orchestrator.provider_health import note_production_tripwire
        note_production_tripwire("web_search", "api.brave.search", drift_kind, evidence)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug(
            "web_search: tripwire write failed", exc_info=True
        )
