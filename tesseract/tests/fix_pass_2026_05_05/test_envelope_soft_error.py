"""Layer 1.5 (2026-05-05) — `stream_error` envelope must round-trip the
structured `raw` payload that `FallbackAdapter` attaches to post-commit
ERROR chunks (`severity='soft'`, `kind`, `model`, `chain_index`,
`provider_error`, `request_id`). Without this, the Mirror frontend can't
distinguish a recoverable provider hiccup from a turn-killing failure
and the operator sees a giant red card on every transient stream
glitch.
"""

from __future__ import annotations

from tesseract.kernel.adapters.base import ChunkType, ErrorKind, StreamChunk
from tesseract.mirror.server.envelope import chunk_to_envelope


def test_soft_error_chunk_round_trips_structured_payload() -> None:
    chunk = StreamChunk(
        type=ChunkType.ERROR,
        error="adapter idx=0 (gpt-5.4-mini) ERROR chunk after commit: req_abc...",
        error_kind=ErrorKind.TRANSIENT,
        raw={
            "severity": "soft",
            "kind": "post_commit_partial",
            "model": "gpt-5.4-mini",
            "chain_index": 0,
            "provider_error": "OpenAI Responses error: ...",
            "request_id": "req_abc1234567890def123456",
        },
    )
    env = chunk_to_envelope(chunk, session_id="s1")
    assert env is not None
    assert env["type"] == "stream_error"
    data = env["data"]
    assert data["severity"] == "soft"
    assert data["kind"] == "post_commit_partial"
    assert data["model"] == "gpt-5.4-mini"
    assert data["chain_index"] == 0
    assert data["provider_error"].startswith("OpenAI Responses error")
    assert data["request_id"] == "req_abc1234567890def123456"
    # Original message kept for back-compat with envelope readers that
    # only look at `message`.
    assert "after commit" in data["message"]


def test_legacy_error_chunk_unchanged() -> None:
    """A pre-`raw`-payload ERROR chunk (no severity, no kind) must still
    produce the minimal envelope shape — no spurious None-valued fields."""
    chunk = StreamChunk(type=ChunkType.ERROR, error="boom")
    env = chunk_to_envelope(chunk, session_id="s1")
    assert env is not None
    data = env["data"]
    assert data == {"message": "boom"}


def test_warning_severity_still_passes_through() -> None:
    """Cost-cap / budget-exhausted warnings (severity='warning') keep
    their existing envelope contract — the new soft branch must not have
    blocked the warning path."""
    chunk = StreamChunk(
        type=ChunkType.ERROR,
        error="budget exhausted",
        raw={"severity": "warning", "reason": "consecutive_adapter_errors"},
    )
    env = chunk_to_envelope(chunk, session_id="s1")
    assert env is not None
    assert env["data"]["severity"] == "warning"
