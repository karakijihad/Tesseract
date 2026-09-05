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

# Kinds that no amount of waiting fixes. A spent account changes when a person
# adds credits, a refused credential when a person signs in, and a rejected
# parameter when a person edits it — none of the three is a function of time,
# so retrying any of them spends latency to reach the same answer.
#
# This replaces four substrings that only ever ran against a 429. They missed
# `You have no credits remaining. Add credits to continue using the API`,
# which is not an exotic phrasing — it is what OpenAI says — and the miss cost
# 60 calls, 20 chain advances and about four seconds a turn on 2026-08-29
# before the chain reached an entry that could answer. The vocabulary that
# reads that sentence correctly already existed; it was simply not asked.
_NEVER_RETRY: frozenset[str] = frozenset({"usage", "auth", "not_found", "schema"})

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


def _never_retry(text: str | bytes | None) -> bool:
    """Whether the provider's own words name a failure that retrying cannot fix.

    One reader, `orchestrator/provider_failure.py::classify`, shared with the
    probes, the CLI auth check, the drift record and the watchman's headline,
    so a sentence is read once and every one of them gets the same word.
    """
    if not text:
        return False
    from tesseract.orchestrator import provider_failure

    haystack = text.decode("utf-8", errors="ignore") if isinstance(text, bytes) else str(text)
    return provider_failure.classify(haystack) in _NEVER_RETRY


def classify_status_code(status: int | None, body: str | bytes | None = None) -> ErrorKind:
    """Classify an HTTP status code, with the body able to harden the answer.

    **The body may only make the decision harder, never softer.** A status code
    is a claim about the transport and the sentence beside it is a claim about
    the account, and letting the sentence soften the code would retry against
    a door that is closed: a 403 whose body happens to mention an overloaded
    service is still a 403. So the words are consulted first and only for the
    kinds that never succeed on a retry; everything else is decided by the
    code exactly as before.
    """
    if _never_retry(body):
        return ErrorKind.HARD
    if status is None:
        return ErrorKind.UNKNOWN
    if status in _HARD_STATUS_CODES:
        return ErrorKind.HARD
    if status in _TRANSIENT_STATUS_CODES:
        return ErrorKind.TRANSIENT
    if 500 <= status < 600:
        return ErrorKind.TRANSIENT
    if 400 <= status < 500:
        return ErrorKind.HARD
    return ErrorKind.UNKNOWN


#: What each classified kind means for the retry decision. Total over
#: `provider_failure.CLASSIFIED_KINDS`, and a test holds it that way, because
#: a kind added to the vocabulary and not to this table would silently take
#: whichever answer the fallback happened to give it.
_RETRY_BY_KIND: dict[str, ErrorKind] = {
    "usage": ErrorKind.HARD,
    "auth": ErrorKind.HARD,
    "not_found": ErrorKind.HARD,
    "schema": ErrorKind.HARD,
    "rate_limit": ErrorKind.TRANSIENT,
    "server": ErrorKind.TRANSIENT,
    "unknown": ErrorKind.UNKNOWN,
}


def retry_kind_for_fault(kind: str) -> ErrorKind:
    """Whether a failure of this kind is worth trying again.

    Raises on a kind nobody placed. That is deliberate and it is AR-17's rule:
    a default is how a new kind goes quiet, and the one thing worse than an
    unclassified failure is a misclassified one that nothing complains about.
    """
    return _RETRY_BY_KIND[kind]


def _status_and_body(exc: BaseException) -> tuple[int | None, Any]:
    """The status code and response body an SDK exception is carrying, if any.

    Provider SDKs put them in three different places between them; httpx wraps
    the response on the exception instead.
    """
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if not isinstance(status, int) and response is not None:
        status = getattr(response, "status_code", None)
    body: Any = getattr(exc, "body", None)
    if body is None and response is not None:
        try:
            body = response.text  # httpx Response
        except Exception:
            body = None
    return (status if isinstance(status, int) else None), body


def classify_exception(exc: BaseException) -> ErrorKind:
    """Classify a raised exception by its own words, its status, then its class."""
    if isinstance(exc, asyncio.TimeoutError):
        return ErrorKind.TRANSIENT

    name = type(exc).__name__
    status, body = _status_and_body(exc)

    # The words first, and BOTH places they hide. Which of the two carries the
    # sentence is the SDK's choice: OpenAI raises `RateLimitError` whose
    # message reads `You have no credits remaining` while the body is a dict,
    # and reading only the body is how that sentence was retried sixty times.
    if _never_retry(" ".join((str(exc), "" if body is None else str(body)))):
        return ErrorKind.HARD

    if status is not None:
        kind = classify_status_code(status, body)
        if kind is not ErrorKind.UNKNOWN:
            return kind

    if name in _HARD_EXC_NAMES:
        return ErrorKind.HARD
    if name in _TRANSIENT_EXC_NAMES:
        return ErrorKind.TRANSIENT

    # Last resort: the shared reader over the message. Many SDKs raise a plain
    # `RuntimeError` with the whole story in its text. This was a private list
    # of seven substrings — a fifth partial copy of a vocabulary that already
    # existed one import away.
    from tesseract.orchestrator import provider_failure

    return retry_kind_for_fault(provider_failure.classify(str(exc)))
