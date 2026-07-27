"""Workstream B — every adapter must classify pre-commit errors with
an `ErrorKind`. The chain routes off this; mis-classification turns a
transient blip into a permanent fallback (or worse, retries a 401).

This file exercises the central classifier (`classify_status_code` /
`classify_exception`) — the one helper every adapter delegates to —
against representative inputs from each provider's SDK.
"""

from __future__ import annotations

import asyncio

import pytest

from tesseract.kernel.adapters.base import ErrorKind
from tesseract.kernel.adapters.errors import (
    classify_exception,
    classify_status_code,
)


# ---------- classify_status_code ----------------------------------------

@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 529])
def test_status_code_transient(status: int) -> None:
    assert classify_status_code(status) == ErrorKind.TRANSIENT


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 422])
def test_status_code_hard(status: int) -> None:
    assert classify_status_code(status) == ErrorKind.HARD


def test_status_code_429_with_quota_body_is_hard() -> None:
    """A 429 carrying `insufficient_quota` is a billing fail — retrying
    it just makes the operator angrier and burns budget elsewhere."""
    body = '{"error":{"type":"insufficient_quota","message":"You exceeded your current quota"}}'
    assert classify_status_code(429, body) == ErrorKind.HARD


def test_status_code_429_plain_throttle_remains_transient() -> None:
    body = '{"error":{"type":"rate_limit_exceeded","message":"slow down"}}'
    assert classify_status_code(429, body) == ErrorKind.TRANSIENT


def test_status_code_unknown_5xx_classified_transient() -> None:
    """5xx not in the explicit set still treated as transient — server
    issues should not strand the chain on a fallback for a 599."""
    assert classify_status_code(599) == ErrorKind.TRANSIENT


def test_status_code_unknown_4xx_classified_hard() -> None:
    """4xx not in the explicit set still treated as hard — retrying a
    426 Upgrade Required against the same endpoint will not succeed."""
    assert classify_status_code(418) == ErrorKind.HARD


def test_status_code_none_is_unknown() -> None:
    assert classify_status_code(None) == ErrorKind.UNKNOWN


# ---------- classify_exception ------------------------------------------

def test_asyncio_timeout_is_transient() -> None:
    assert classify_exception(asyncio.TimeoutError()) == ErrorKind.TRANSIENT


def test_connection_error_is_transient() -> None:
    """Plain `ConnectionError` from urllib3 / network stack — the next
    request might land. Retry."""
    assert classify_exception(ConnectionError("upstream dropped")) == ErrorKind.TRANSIENT


def test_status_code_attribute_overrides_class_name() -> None:
    """Provider SDKs sometimes raise `RateLimitError` for a 429 that
    actually carries `insufficient_quota` — the attribute wins so we
    correctly classify HARD, not TRANSIENT."""
    class RateLimitError(Exception):
        status_code = 429
        body = '{"error":{"type":"insufficient_quota"}}'

    assert classify_exception(RateLimitError("quota gone")) == ErrorKind.HARD


def test_provider_sdk_class_names_classified() -> None:
    """Class-name fallback path — used when the SDK does not attach a
    status code (or attaches None). Mirrors the OpenAI / Anthropic /
    google.genai shapes."""
    class APIConnectionError(Exception):
        pass

    class AuthenticationError(Exception):
        pass

    class NotFoundError(Exception):
        pass

    class OverloadedError(Exception):
        pass  # Anthropic 529

    assert classify_exception(APIConnectionError("net")) == ErrorKind.TRANSIENT
    assert classify_exception(AuthenticationError("bad key")) == ErrorKind.HARD
    assert classify_exception(NotFoundError("model gone")) == ErrorKind.HARD
    assert classify_exception(OverloadedError("529")) == ErrorKind.TRANSIENT


def test_httpx_response_attribute_classified() -> None:
    """httpx-style: exception carries a `.response` with `.status_code`.
    The classifier walks through to the response."""
    class _Resp:
        def __init__(self, code: int, text: str = "") -> None:
            self.status_code = code
            self.text = text

    class HTTPStatusError(Exception):
        def __init__(self, code: int, text: str = "") -> None:
            super().__init__(f"http {code}")
            self.response = _Resp(code, text)

    assert classify_exception(HTTPStatusError(503)) == ErrorKind.TRANSIENT
    assert classify_exception(HTTPStatusError(401)) == ErrorKind.HARD
    assert classify_exception(HTTPStatusError(429, "slow down")) == ErrorKind.TRANSIENT
    assert (
        classify_exception(HTTPStatusError(429, '{"type":"insufficient_quota"}'))
        == ErrorKind.HARD
    )


def test_message_heuristic_last_resort() -> None:
    """Bare `RuntimeError` with a status word in the message — the last
    resort. Keeps the chain alive when an adapter forgets to set
    `error_kind` and the SDK doesn't expose a status."""
    assert classify_exception(RuntimeError("connection reset by peer")) == ErrorKind.TRANSIENT
    assert classify_exception(RuntimeError("request timed out")) == ErrorKind.TRANSIENT
    assert classify_exception(RuntimeError("invalid api key")) == ErrorKind.HARD
    assert classify_exception(RuntimeError("403 forbidden")) == ErrorKind.HARD


def test_unrecognized_exception_is_unknown() -> None:
    """Adapters that emit ERROR with `error_kind=None` and the message
    matches no heuristic → UNKNOWN. The chain treats UNKNOWN as
    TRANSIENT but the classifier itself does not lie."""
    class _MysteryError(Exception):
        pass

    assert classify_exception(_MysteryError("no recognizable signal")) == ErrorKind.UNKNOWN
