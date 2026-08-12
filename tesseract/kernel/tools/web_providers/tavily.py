"""TavilyProvider family — shared Tavily API identity for `tavily_search`
and `tavily_extract`. Both operations share auth, the env var name, and
the free-tier quota / error text; only the endpoint, request payload,
and response shape differ per operation, so each gets its own thin
subclass off a common base.

Free key (1K req/mo) at https://tavily.com.
"""

from __future__ import annotations

from typing import Any

import httpx

from tesseract.kernel.tools.web_providers.base import WebSearchProvider

_MISSING_KEY_HINT = (
    "TAVILY_API_KEY not set in .env. Get a free key (1K/mo) at "
    "https://tavily.com and add TAVILY_API_KEY=... to tesseract/.env"
)


class _TavilyProviderBase(WebSearchProvider):
    api_key_env = "TAVILY_API_KEY"
    service = "tavily"
    http_method = "POST"

    def missing_key_message(self) -> str:
        return _MISSING_KEY_HINT

    def auth_headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def unauthorized_message(self) -> str:
        return "Tavily 401 — API key rejected. Check TAVILY_API_KEY."

    def rate_limited_message(self) -> str:
        return "Tavily 429 — rate limit exceeded (1K/mo on the free tier)."

    def http_error_message(self, status_code: int, body: str) -> str:
        return f"Tavily {status_code}: {body}"

    def non_json_message(self) -> str:
        return "Tavily returned non-JSON"


class TavilySearchProvider(_TavilyProviderBase):
    endpoint = "https://api.tavily.com/search"
    tripwire_source = "api.tavily.search"

    def build_request(self, inp: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        return payload

    def parse_results(self, data: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        return data.get("results") or [], data.get("answer") or ""

    def timeout_message(self, timeout: float) -> str:
        return f"Tavily search timed out after {timeout}s"

    def request_error_message(self, exc: httpx.HTTPError) -> str:
        return f"Tavily search request failed: {exc}"


class TavilyExtractProvider(_TavilyProviderBase):
    endpoint = "https://api.tavily.com/extract"
    tripwire_source = "api.tavily.extract"

    def build_request(self, inp: Any) -> dict[str, Any]:
        return {"urls": inp.urls, "extract_depth": inp.extract_depth}

    def parse_results(self, data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return data.get("results") or [], data.get("failed_results") or []

    def timeout_message(self, timeout: float) -> str:
        return f"Tavily extract timed out after {timeout}s"

    def request_error_message(self, exc: httpx.HTTPError) -> str:
        return f"Tavily extract request failed: {exc}"
