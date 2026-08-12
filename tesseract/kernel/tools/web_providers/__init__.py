"""Shared web-search/extract provider abstraction.

`web_search`, `tavily_search`, and `tavily_extract` all follow the same
shape: read an API key from the environment, build an authenticated
request, call out over HTTP, map the status code to an operator-facing
error, and note a production tripwire on failure. Each concrete
provider (see `brave.py` / `tavily.py`) owns everything that varies by
vendor: the endpoint, the auth header shape, the API-key env var name,
request-param construction, response parsing, the status-code ->
error-message mapping, and the tripwire source label. The Tool classes
that consume these providers are thin shells — see `web_search.py`,
`tavily_search.py`, `tavily_extract.py`.
"""

from __future__ import annotations

from tesseract.kernel.tools.web_providers.base import (
    FetchOutcome,
    WebSearchProvider,
    fetch_json,
    service_disabled_reason,
    service_key_env,
)

__all__ = [
    "FetchOutcome",
    "WebSearchProvider",
    "fetch_json",
    "service_disabled_reason",
    "service_key_env",
]
