"""Context7LookupTool — fetch up-to-date library docs via Context7 HTTP API.

Two-phase: resolve library name → Context7 ID, then fetch docs scoped by topic.
If `library` already looks like a Context7 ID (/owner/repo), the resolve phase
is skipped.

Auth is optional — public access works without a key. Set CONTEXT7_API_KEY in
.env to unlock higher rate limits.

Endpoints (v2):
  Search:  GET https://context7.com/api/v2/libs/search?libraryName=<name>&query=<topic>
  Context: GET https://context7.com/api/v2/context?libraryId=<id>&query=<topic>&type=txt
"""

from __future__ import annotations

import os
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field

from tesseract import http_client
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult

_BASE = "https://context7.com/api"
_SEARCH_URL = f"{_BASE}/v2/libs/search"
_CONTEXT_URL = f"{_BASE}/v2/context"
_TIMEOUT = 15.0
_MAX_OUTPUT_CHARS = 20_000


class Context7LookupInput(BaseModel):
    library: str = Field(description="Library name (e.g. 'fastapi', 'react') or Context7 ID (e.g. '/tiangolo/fastapi')")
    topic: str = Field(default="", description="Optional topic filter, e.g. 'routing', 'hooks', 'authentication'")
    tokens: int = Field(default=5000, ge=500, le=20000, description="Doc length budget in tokens (500–20000)")


def _auth_headers() -> dict[str, str]:
    key = os.environ.get("CONTEXT7_API_KEY")
    if key:
        return {"Authorization": f"Bearer {key}"}
    return {}


def _is_library_id(value: str) -> bool:
    return value.startswith("/") and value.count("/") >= 2


class Context7LookupTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "searching-the-web"
    summary: ClassVar[str] = "Fetches current library or framework API docs from Context7 by name or ID."
    use_when: ClassVar[str] = (
        "Use for any library or framework API question instead of guessing from training "
        "knowledge — resolves a name to a Context7 ID and fetches docs scoped by topic."
    )
    not_when: ClassVar[str] = (
        "Use `web_search` or `tavily_search` for anything outside library documentation, "
        "such as news or general topics."
    )

    @property
    def name(self) -> str:
        return "context7_lookup"

    @property
    def input_schema(self) -> type[BaseModel]:
        return Context7LookupInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, Context7LookupInput) else Context7LookupInput(**tool_input.model_dump())

        headers = _auth_headers()
        query = inp.topic or inp.library

        async with http_client.async_client(timeout=_TIMEOUT) as client:
            library_id = await self._resolve_id(client, inp.library, query, headers)
            if isinstance(library_id, ToolResult):
                return library_id

            return await self._fetch_docs(client, library_id, query, inp.tokens, headers)

    async def _resolve_id(
        self,
        client: httpx.AsyncClient,
        library: str,
        query: str,
        headers: dict[str, str],
    ) -> str | ToolResult:
        if _is_library_id(library):
            return library

        try:
            r = await client.get(
                _SEARCH_URL,
                params={"libraryName": library, "query": query},
                headers=headers,
            )
        except httpx.TimeoutException:
            return ToolResult(output=f"Context7 search timed out after {_TIMEOUT}s", is_error=True)
        except httpx.HTTPError as e:
            return ToolResult(output=f"Context7 search request failed: {e}", is_error=True)

        if r.status_code == 429:
            return ToolResult(output="Context7 rate limit hit. Set CONTEXT7_API_KEY for higher limits.", is_error=True)
        if r.status_code >= 400:
            return ToolResult(output=f"Context7 search {r.status_code}: {r.text[:200]}", is_error=True)

        try:
            data = r.json()
        except ValueError:
            return ToolResult(output="Context7 search returned non-JSON", is_error=True)

        results = data.get("results") or []
        if not results:
            return ToolResult(output=f"Context7: no libraries found for '{library}'. Try a more specific name.")

        return results[0]["id"]

    async def _fetch_docs(
        self,
        client: httpx.AsyncClient,
        library_id: str,
        query: str,
        tokens: int,
        headers: dict[str, str],
    ) -> ToolResult:
        try:
            r = await client.get(
                _CONTEXT_URL,
                params={"libraryId": library_id, "query": query, "tokens": tokens, "type": "txt"},
                headers=headers,
            )
        except httpx.TimeoutException:
            return ToolResult(output=f"Context7 docs timed out after {_TIMEOUT}s", is_error=True)
        except httpx.HTTPError as e:
            return ToolResult(output=f"Context7 docs request failed: {e}", is_error=True)

        if r.status_code == 202:
            return ToolResult(output=f"Context7: library '{library_id}' is still being indexed. Try again shortly.")
        if r.status_code == 404:
            return ToolResult(output=f"Context7: library ID '{library_id}' not found.", is_error=True)
        if r.status_code == 429:
            return ToolResult(output="Context7 rate limit hit. Set CONTEXT7_API_KEY for higher limits.", is_error=True)
        if r.status_code >= 400:
            return ToolResult(output=f"Context7 {r.status_code}: {r.text[:200]}", is_error=True)

        text = r.text.strip()
        if not text:
            return ToolResult(output=f"Context7: no docs returned for '{library_id}' / topic '{query}'.")

        header = f"Library: {library_id}\n\n"
        body = text
        if len(header) + len(body) > _MAX_OUTPUT_CHARS:
            body = body[: _MAX_OUTPUT_CHARS - len(header) - 14] + "\n… [truncated]"

        return ToolResult(
            output=header + body,
            metadata={"library_id": library_id, "query": query, "tokens": tokens},
        )
