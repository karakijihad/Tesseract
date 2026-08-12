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


#: `(mtime_ns, services block)`. `load_bundle()` re-reads providers.yaml and
#: roles.yaml on every call by design, which measured **186 ms** here — this
#: check runs before every search and before every inbound message carrying a
#: link, against an event-loop budget of 50 ms. A stat is microseconds
#: and the config watcher's edit changes the mtime, so freshness survives.
_SERVICES_CACHE: tuple[int, dict[str, Any]] | None = None


def _services_block() -> dict[str, Any] | None:
    global _SERVICES_CACHE

    try:
        import yaml

        from tesseract.paths import config_dir

        path = config_dir() / "providers.yaml"
        stamp = path.stat().st_mtime_ns
        if _SERVICES_CACHE is None or _SERVICES_CACHE[0] != stamp:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            block = raw.get("services")
            _SERVICES_CACHE = (stamp, block if isinstance(block, dict) else {})
        return _SERVICES_CACHE[1]
    except Exception:  # noqa: BLE001 — availability beats strictness here
        return None


def service_key_env(service: str, default: str) -> str:
    """The env var name `providers.yaml::services.<service>.api_key_env`
    declares, or `default` when the catalog does not say.

    The declaration exists so the settings view and the setup form can tell
    the operator which key a capability needs; if the code that resolves the
    credential ignored it, the file would be describing something it does
    not control. Same reasoning as `channels.yaml` and its channel tokens.
    """
    block = (_services_block() or {}).get(service)
    if isinstance(block, dict) and block.get("api_key_env"):
        return str(block["api_key_env"])
    return default


def service_disabled_reason(service: str) -> str | None:
    """Why `providers.yaml::services.<service>` is off, or None when it is on.

    Mirrors how a disabled provider is skipped in `brain/boot.py` — the
    section switch and the per-service switch are checked separately, and the
    message names the one that is false, so the operator is told which line
    to edit rather than that "it is off".

    Read at call time, not at boot: the config watcher reloads this file
    live, so switching a service back on takes effect on the next turn.
    Absent config, or an unreadable catalog, means enabled — a tool must not
    go dark because a file could not be parsed.
    """
    raw = _services_block()
    if not raw:
        return None

    if not raw.get("enabled", True):
        return "every service is switched off in providers.yaml (services.enabled: false)"
    block = raw.get(service)
    if isinstance(block, dict) and not block.get("enabled", True):
        return (
            f"switched off in providers.yaml (services.{service}.enabled: false) — "
            "set it to true to use this again"
        )
    return None


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
    #: The `providers.yaml::services` block this provider belongs to. What
    #: makes the catalog's `enabled` switch real for a tool: a key present
    #: and a service switched off means the operator wants it off.
    service: str

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a bad `http_method` at class-definition time.

        This used to be checked inside `fetch_json`, which is the wrong
        altitude twice over: the failure arrived per request rather than once,
        after an `httpx.AsyncClient` had been built for a call that was never
        going to be sent, and as an uncaught traceback where every other
        invalid-input path in this package returns a `ToolResult`. The value is
        a class attribute written by hand, so it is knowable at import — and a
        provider that cannot send a request should not survive being defined.

        Abstract intermediates are skipped: only a class that declares the
        attribute is checked, so a subclass hierarchy may fill it in later.
        """
        super().__init_subclass__(**kwargs)
        method = getattr(cls, "http_method", None)
        if method is not None and method not in ("GET", "POST"):
            raise ValueError(
                f"{cls.__name__}.http_method must be 'GET' or 'POST', got {method!r}"
            )

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
                # Unreachable: `WebSearchProvider.__init_subclass__` refuses
                # any other value at class-definition time. Kept as an
                # assertion so the branch cannot fall through silently and
                # POST a GET provider's query params as a JSON body.
                raise AssertionError(
                    f"{type(provider).__name__}.http_method passed class-time "
                    f"validation but is {provider.http_method!r}"
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
