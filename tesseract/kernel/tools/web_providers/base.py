"""`WebSearchProvider` — the interface every web-search/extract vendor
implements, plus `fetch_json`, the shared GET/POST -> status-check ->
JSON-decode skeleton every tool in this package drives it through.

Vendor identity (the words "Brave"/"Tavily", signup URLs, free-tier
quotas) lives only in the concrete provider modules (`brave.py`,
`tavily.py`) — never here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Literal

import httpx

from tesseract.kernel.tools.base import ToolResult

# Called as `note_tripwire(drift_kind, evidence)` on every failure branch.
# Optional: a tool passing `None` gets identical error handling with no
# telemetry write. Every shipped tool wires one.
NoteTripwire = Callable[[str, dict[str, Any]], None]


class WebSearchProvider(ABC):
    """What varies between web-search/extract vendors.

    One instance per Tool (`web_search` <-> BraveProvider,
    `tavily_search` <-> TavilySearchProvider, `tavily_extract` <->
    TavilyExtractProvider). Each captures the endpoint, the auth header
    shape, the API-key env var name, request-param construction,
    response parsing, the status-code -> error-message mapping, and the
    tripwire source label for its one operation.
    """

    api_key_env: str
    endpoint: str
    http_method: Literal["GET", "POST"]
    tripwire_source: str

    @abstractmethod
    def missing_key_message(self) -> str: ...

    @abstractmethod
    def auth_headers(self, api_key: str) -> dict[str, str]: ...

    @abstractmethod
    def build_request(self, inp: Any) -> dict[str, Any]:
        """Return the GET query params or POST JSON payload for `inp`."""

    @abstractmethod
    def parse_results(self, data: dict[str, Any]) -> Any:
        """Pull the operation's meaningful fields out of the decoded JSON body."""

    @abstractmethod
    def timeout_message(self, timeout: float) -> str: ...

    @abstractmethod
    def request_error_message(self, exc: httpx.HTTPError) -> str: ...

    @abstractmethod
    def unauthorized_message(self) -> str: ...

    @abstractmethod
    def rate_limited_message(self) -> str: ...

    @abstractmethod
    def http_error_message(self, status_code: int, body: str) -> str: ...

    @abstractmethod
    def non_json_message(self) -> str: ...


@dataclass(frozen=True)
class FetchOutcome:
    """Either `data` (the decoded JSON body) or `error` (a ready
    `ToolResult` for the tool to return as-is) is set — never both."""

    data: dict[str, Any] | None = None
    error: ToolResult | None = None


async def fetch_json(
    provider: WebSearchProvider,
    *,
    api_key: str,
    request: dict[str, Any],
    timeout: float,
    note_tripwire: NoteTripwire | None = None,
) -> FetchOutcome:
    """Send `provider`'s request, map failures to operator-facing
    messages, and decode the JSON body. Shared by every tool in this
    package so the GET/POST -> status-check -> decode shape lives in
    exactly one place."""
    headers = provider.auth_headers(api_key)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if provider.http_method == "GET":
                r = await client.get(provider.endpoint, headers=headers, params=request)
            elif provider.http_method == "POST":
                r = await client.post(provider.endpoint, headers=headers, json=request)
            else:
                # Fail where the method is declared, not at the vendor: an
                # unrecognised value silently POSTing a GET provider's query
                # params as a JSON body surfaces as a vendor 4xx.
                raise ValueError(
                    f"{type(provider).__name__}.http_method must be 'GET' or "
                    f"'POST', got {provider.http_method!r}"
                )
    except httpx.TimeoutException:
        if note_tripwire:
            note_tripwire("latency_spike", {"timeout_seconds": timeout})
        return FetchOutcome(error=ToolResult(output=provider.timeout_message(timeout), is_error=True))
    except httpx.HTTPError as e:
        if note_tripwire:
            note_tripwire("http_error", {"exception": repr(e)})
        return FetchOutcome(error=ToolResult(output=provider.request_error_message(e), is_error=True))

    if r.status_code == 401:
        if note_tripwire:
            note_tripwire("unavailable", {"status_code": 401})
        return FetchOutcome(error=ToolResult(output=provider.unauthorized_message(), is_error=True))
    if r.status_code == 429:
        if note_tripwire:
            note_tripwire("http_error", {"status_code": 429, "reason": "rate limit"})
        return FetchOutcome(error=ToolResult(output=provider.rate_limited_message(), is_error=True))
    if r.status_code >= 400:
        body = r.text[:200]
        if note_tripwire:
            note_tripwire("http_error", {"status_code": r.status_code, "body": body})
        return FetchOutcome(error=ToolResult(output=provider.http_error_message(r.status_code, body), is_error=True))

    try:
        data = r.json()
    except ValueError:
        if note_tripwire:
            note_tripwire("shape_mismatch", {"reason": "non-JSON response"})
        return FetchOutcome(error=ToolResult(output=provider.non_json_message(), is_error=True))

    return FetchOutcome(data=data)
