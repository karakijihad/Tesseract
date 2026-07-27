"""Adapter error classification — single source for `ErrorKind` mapping.

`FallbackAdapter` routes pre-commit `ERROR` chunks based on their
`error_kind`:

- `TRANSIENT`: retry the same chain entry up to
  `chain.transient_retries` times before advancing.
- `HARD`: advance immediately, no retry.
- `UNKNOWN`: treated as `TRANSIENT` (safe default — retry first).

Concrete adapters classify per-error at the ERROR-emit sites and pass
the result into `StreamChunk(error_kind=...)`. Putting the rules here
keeps every adapter's behavior consistent and reviewable in one file.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tesseract.kernel.adapters.base import ErrorKind

# Status codes that warrant a retry against the same provider — the call
# may succeed if we wait. 408 Request Timeout, 425 Too Early, 429 Too
# Many Requests (unless quota), 500/502/503/504 server-side wobble,
# 529 Anthropic-specific overloaded.
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

# Status codes that retrying does not help. 400 malformed body (incl.
# context-window overflow), 401 auth, 403 forbidden / billing inactive,
# 404 model not found, 409 conflict, 410 gone, 422 unprocessable.
_HARD_STATUS_CODES = frozenset({400, 401, 403, 404, 409, 410, 422})

# Substrings (case-insensitive) within an error body or message that
# downgrade a 429 from TRANSIENT to HARD — quota exhausted means more
# requests will not succeed without operator action.
_HARD_429_SUBSTRINGS = (
    "insufficient_quota",
    "quota exceeded",
    "billing",
    "exceeded your current quota",
)

# Class names from provider SDKs (openai, anthropic, google.genai,
# httpx). Matched on `type(exc).__name__` so we don't have to import
# each SDK's class hierarchy. Kept here so reviewers can audit the
# whole list in one place.
_TRANSIENT_EXC_NAMES = frozenset({
    "APIConnectionError",
    "APIConnectionTimeoutError",
    "APITimeoutError",
    "InternalServerError",       # openai 5xx
    "ServiceUnavailableError",   # openai 503 alias
    "RateLimitError",            # default-transient unless body says quota
    "OverloadedError",            # anthropic-specific 529
    "ConnectError",              # httpx
    "ReadError",                 # httpx
    "ReadTimeout",               # httpx
    "ConnectTimeout",            # httpx
    "WriteTimeout",              # httpx
    "PoolTimeout",               # httpx
    "TimeoutException",          # httpx base
    "RemoteProtocolError",       # httpx — connection dropped
    "ConnectionError",           # generic / urllib3
    "TimeoutError",              # generic / asyncio
})

_HARD_EXC_NAMES = frozenset({
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "BadRequestError",
    "UnprocessableEntityError",
    "ConflictError",
    "ContentFilterFinishReasonError",
})


def classify_status_code(status: int | None, body: str | bytes | None = None) -> ErrorKind:
    """Classify an HTTP status code (with optional body for 429 nuance)."""
    if status is None:
        return ErrorKind.UNKNOWN
    if status in _HARD_STATUS_CODES:
        return ErrorKind.HARD
    if status == 429 and body:
        text = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else str(body)
        lowered = text.lower()
        if any(s in lowered for s in _HARD_429_SUBSTRINGS):
            return ErrorKind.HARD
        return ErrorKind.TRANSIENT
    if status in _TRANSIENT_STATUS_CODES:
        return ErrorKind.TRANSIENT
    if 500 <= status < 600:
        return ErrorKind.TRANSIENT
    if 400 <= status < 500:
        return ErrorKind.HARD
    return ErrorKind.UNKNOWN


def classify_exception(exc: BaseException) -> ErrorKind:
    """Classify a raised exception by class name + duck-typed status."""
    if isinstance(exc, asyncio.TimeoutError):
        return ErrorKind.TRANSIENT

    name = type(exc).__name__

    # Provider SDKs commonly attach a `status_code` to their error
    # classes. Trust it over the class name when present — a
    # `RateLimitError` with `insufficient_quota` body should be HARD.
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        # httpx wraps the response on the exception instance.
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
    if isinstance(status, int):
        body: Any = getattr(exc, "body", None)
        if body is None:
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    body = response.text  # httpx Response
                except Exception:
                    body = None
        kind = classify_status_code(status, body)
        if kind is not ErrorKind.UNKNOWN:
            return kind

    if name in _HARD_EXC_NAMES:
        return ErrorKind.HARD
    if name in _TRANSIENT_EXC_NAMES:
        return ErrorKind.TRANSIENT

    # Last-resort string heuristic. Many SDKs raise plain
    # `RuntimeError` / `Exception` with the status word in the message.
    msg = str(exc).lower()
    if any(w in msg for w in ("connection", "connect error", "timeout", "timed out", "temporarily unavailable")):
        return ErrorKind.TRANSIENT
    if any(w in msg for w in ("unauthorized", "forbidden", "not found", "invalid api key", "authentication")):
        return ErrorKind.HARD

    return ErrorKind.UNKNOWN
