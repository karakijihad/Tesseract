"""BraveProvider — Brave Search API identity for `web_search`.

Free key (2K req/mo) at https://brave.com/search/api/.
"""

from __future__ import annotations

from typing import Any

import httpx

from tesseract.kernel.tools.web_providers.base import WebSearchProvider

_MISSING_KEY_HINT = (
    "BRAVE_SEARCH_API_KEY not set in .env. Get a free key at "
    "https://brave.com/search/api/ and add BRAVE_SEARCH_API_KEY=... to tesseract/.env"
)


class BraveProvider(WebSearchProvider):
    api_key_env = "BRAVE_SEARCH_API_KEY"
    service = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"
    http_method = "GET"
    tripwire_source = "api.brave.search"

    def missing_key_message(self) -> str:
        return _MISSING_KEY_HINT

    def auth_headers(self, api_key: str) -> dict[str, str]:
        return {"X-Subscription-Token": api_key, "Accept": "application/json"}

    def build_request(self, inp: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"q": inp.query, "count": inp.count}
        if inp.country:
            params["country"] = inp.country
        return params

    def parse_results(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        return (data.get("web") or {}).get("results") or []

    def timeout_message(self, timeout: float) -> str:
        return f"Brave Search timed out after {timeout}s"

    def request_error_message(self, exc: httpx.HTTPError) -> str:
        return f"Brave Search request failed: {exc}"

    def unauthorized_message(self) -> str:
        return "Brave Search 401 — API key rejected. Check BRAVE_SEARCH_API_KEY."

    def rate_limited_message(self) -> str:
        return "Brave Search 429 — rate limit exceeded (2K/mo on the free tier)."

    def http_error_message(self, status_code: int, body: str) -> str:
        return f"Brave Search {status_code}: {body}"

    def non_json_message(self) -> str:
        return "Brave Search returned non-JSON"
