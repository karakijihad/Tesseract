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

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.web_providers import (
    fetch_json,
    service_disabled_reason,
    service_key_env,
)
from tesseract.kernel.tools.web_providers.brave import BraveProvider

_DEFAULT_TIMEOUT = 10.0
_MAX_RESULTS = 10

_PROVIDER = BraveProvider()


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

    group: ClassVar[str] = "searching-the-web"
    summary: ClassVar[str] = "Web search via Brave — a ranked list of title, URL, and short snippet."
    use_when: ClassVar[str] = (
        "Use for breadth on current events, recent docs, or niche queries — wide results, "
        "not one synthesized answer. The web is not the vault; it holds outside sources."
    )
    not_when: ClassVar[str] = (
        "Use `tavily_search` when you need denser per-result content or a synthesized answer "
        "instead of short snippets."
    )

    @property
    def name(self) -> str:
        return "web_search"

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

        # The operator may hold a key and still want this off; the
        # catalog switch decides, and it is read per call so the
        # config watcher's reload takes effect on the next turn.
        disabled = service_disabled_reason(_PROVIDER.service)
        if disabled is not None:
            return ToolResult(output=f"{self.name}: {disabled}", is_error=True)
        # The catalog names the key; the class constant is only the
        # fallback for a config that does not.
        api_key = os.environ.get(
            service_key_env(_PROVIDER.service, _PROVIDER.api_key_env)
        )
        if not api_key:
            return ToolResult(output=_PROVIDER.missing_key_message(), is_error=True)

        outcome = await fetch_json(
            _PROVIDER,
            api_key=api_key,
            request=_PROVIDER.build_request(inp),
            timeout=_DEFAULT_TIMEOUT,
            note_tripwire=_note_brave_tripwire,
        )
        if outcome.error is not None:
            return outcome.error

        results = _PROVIDER.parse_results(outcome.data or {})
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
        note_production_tripwire("web_search", _PROVIDER.tripwire_source, drift_kind, evidence)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug(
            "web_search: tripwire write failed", exc_info=True
        )
