"""TavilyExtractTool — URL → clean markdown via Tavily extract API.

Use when you have specific URLs and want readable content. Complements
`tavily_search` (which returns snippets) and `web_search` (which returns
titles + descriptions). No equivalent in Brave.

Requires `TAVILY_API_KEY` in .env. 1K req/mo free at tavily.com.
"""

from __future__ import annotations

import os
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult

_ENDPOINT = "https://api.tavily.com/extract"
_TIMEOUT = 30.0  # extract is slower than search
_MAX_URLS = 20
_MAX_OUTPUT_CHARS = 30_000


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

        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            return ToolResult(
                output="TAVILY_API_KEY not set in .env. Get a free key (1K/mo) at https://tavily.com and add TAVILY_API_KEY=... to tesseract/.env",
                is_error=True,
            )

        if inp.extract_depth not in ("basic", "advanced"):
            return ToolResult(output=f"extract_depth must be 'basic' or 'advanced', got {inp.extract_depth!r}", is_error=True)

        payload = {
            "urls": inp.urls,
            "extract_depth": inp.extract_depth,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(_ENDPOINT, headers=headers, json=payload)
        except httpx.TimeoutException:
            return ToolResult(output=f"Tavily extract timed out after {_TIMEOUT}s", is_error=True)
        except httpx.HTTPError as e:
            return ToolResult(output=f"Tavily extract request failed: {e}", is_error=True)

        if r.status_code == 401:
            return ToolResult(output="Tavily 401 — API key rejected. Check TAVILY_API_KEY.", is_error=True)
        if r.status_code == 429:
            return ToolResult(output="Tavily 429 — rate limit exceeded (1K/mo on the free tier).", is_error=True)
        if r.status_code >= 400:
            return ToolResult(output=f"Tavily {r.status_code}: {r.text[:200]}", is_error=True)

        try:
            data = r.json()
        except ValueError:
            return ToolResult(output="Tavily returned non-JSON", is_error=True)

        results = data.get("results") or []
        failed = data.get("failed_results") or []

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
