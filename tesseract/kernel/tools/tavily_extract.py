"""TavilyExtractTool — URL → clean markdown via Tavily extract API.

Use when you have specific URLs and want readable content. Complements
`tavily_search` (which returns snippets) and `web_search` (which returns
titles + descriptions). No equivalent in Brave.

Requires `TAVILY_API_KEY` in .env. 1K req/mo free at tavily.com.
"""

from __future__ import annotations

import logging
import os
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.web_providers import fetch_json
from tesseract.kernel.tools.web_providers.tavily import TavilyExtractProvider

_TIMEOUT = 30.0  # extract is slower than search
_MAX_URLS = 20
_MAX_OUTPUT_CHARS = 30_000

_PROVIDER = TavilyExtractProvider()


class TavilyExtractInput(BaseModel):
    urls: list[str] = Field(description="One or more URLs to extract (up to 20)", min_length=1, max_length=_MAX_URLS)
    extract_depth: str = Field(default="basic", description="'basic' (fast) or 'advanced' (deeper parsing, richer content)")


class TavilyExtractTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"
    # Audit-3 M9 — extracted web pages are attacker-controlled content.
    untrusted_source: ClassVar[bool] = True

    @property
    def name(self) -> str:
        return "tavily_extract"

    @property
    def description(self) -> str:
        return (
            "Extract clean markdown from one or more URLs via Tavily. "
            "Use when you have specific URLs and need readable content — "
            "e.g. after tavily_search / web_search surfaces a promising link."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return TavilyExtractInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, TavilyExtractInput) else TavilyExtractInput(**tool_input.model_dump())

        api_key = os.environ.get(_PROVIDER.api_key_env)
        if not api_key:
            return ToolResult(output=_PROVIDER.missing_key_message(), is_error=True)

        if inp.extract_depth not in ("basic", "advanced"):
            return ToolResult(output=f"extract_depth must be 'basic' or 'advanced', got {inp.extract_depth!r}", is_error=True)

        outcome = await fetch_json(
            _PROVIDER,
            api_key=api_key,
            request=_PROVIDER.build_request(inp),
            timeout=_TIMEOUT,
            note_tripwire=_note_tavily_extract_tripwire,
        )
        if outcome.error is not None:
            return outcome.error

        results, failed = _PROVIDER.parse_results(outcome.data or {})

        if not results:
            msg = "Tavily extract: no URLs succeeded."
            if failed:
                msg += "\nFailures:\n" + "\n".join(f"  {f.get('url','?')} — {f.get('error','?')}" for f in failed)
            return ToolResult(output=msg, is_error=True)

        blocks: list[str] = []
        running_chars = 0
        truncated = False
        for hit in results:
            url = (hit.get("url") or "").strip()
            content = (hit.get("raw_content") or "").strip()
            header = f"\n=== {url} ===\n"
            if running_chars + len(header) + len(content) > _MAX_OUTPUT_CHARS:
                remaining = _MAX_OUTPUT_CHARS - running_chars - len(header) - 20
                if remaining > 0:
                    blocks.append(header + content[:remaining] + "\n… [truncated]")
                truncated = True
                break
            blocks.append(header + content)
            running_chars += len(header) + len(content)

        output = "".join(blocks).lstrip()
        if failed:
            output += "\n\n--- Failed URLs ---\n" + "\n".join(f"  {f.get('url','?')} — {f.get('error','?')}" for f in failed)
        if truncated:
            output += "\n\n[output capped at 30k chars — request fewer URLs or use tavily_search to narrow]"

        return ToolResult(
            output=output,
            metadata={"succeeded": len(results), "failed": len(failed), "depth": inp.extract_depth},
        )


def _note_tavily_extract_tripwire(drift_kind: str, evidence: dict) -> None:
    """Production tripwire — best-effort JSONL row write."""
    try:
        from tesseract.orchestrator.provider_health import note_production_tripwire
        note_production_tripwire("tavily_extract", _PROVIDER.tripwire_source, drift_kind, evidence)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug(
            "tavily_extract: tripwire write failed", exc_info=True
        )
