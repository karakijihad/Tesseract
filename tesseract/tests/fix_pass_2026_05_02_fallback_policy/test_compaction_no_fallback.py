"""Workstream A — compaction must never swap models.

Operator-observed (2026-04-28): auto-compaction silently produced a
summary using a fallback model rather than the configured primary,
then permanently overwrote `ChatSession.history` with that summary.
The summary's voice is load-bearing — it seeds every subsequent turn
until the next compaction.

Fix: `compact_history` unwraps any `FallbackAdapter` to its primary
before streaming. On primary failure the function returns ``""`` and
the caller keeps full history (which retries on the next turn).
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from tesseract.brain.adapter_chain import FallbackAdapter
from tesseract.brain.compaction import compact_history
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


# CR-0 M6: the validator requires the 5 named ## sections. Use a marker
# token IN the structured shape so per-test assertions still distinguish
# "primary" vs "secondary" output without breaking the validator.
def _structured(marker: str) -> str:
    return (
        f"## Operator goals\n- {marker}\n"
        f"## Decisions made\n- {marker}\n"
        f"## Files touched\n- {marker}\n"
        f"## Facts learned\n- {marker}\n"
        f"## Open threads\n- {marker}\n"
    )


class _RecordingAdapter(ModelAdapter):
    def __init__(
        self,
        name: str,
        text: str | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.name = name
        self._text = text if text is not None else _structured(name)
        self._raise_exc = raise_exc
        self.calls = 0

    @property
    def model(self) -> str:
        return self.name

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        yield StreamChunk(type=ChunkType.TEXT, text=self._text)
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _opts(model: str) -> AdapterOptions:
    return AdapterOptions(model=model, provider="test")


_HISTORY = [
    {"role": "user", "content": "what was the plan?"},
    {"role": "assistant", "content": "we agreed on X."},
]


def test_fallback_adapter_uses_primary_only_for_compaction() -> None:
    primary = _RecordingAdapter("primary", text=_structured("primary-summary"))
    secondary = _RecordingAdapter("secondary", text=_structured("should-not-run"))
    fb = FallbackAdapter(
        [(primary, _opts("primary-model")), (secondary, _opts("secondary-model"))],
        transient_retries=0,
        transient_backoff_ms=0,
    )

    summary = asyncio.run(
        compact_history(
            adapter=fb,
            options=_opts("primary-model"),
            history_to_summarize=_HISTORY,
        )
    )

    assert "primary-summary" in summary
    assert "should-not-run" not in summary
    assert primary.calls == 1
    assert secondary.calls == 0, "compaction must never swap to fallback"


def test_compaction_returns_empty_when_primary_fails_no_fallback() -> None:
    """Primary fails → return empty string. Secondary must NEVER run.
    Caller (`ChatSession.compact()`) treats empty as 'keep full history'
    which naturally retries on the next turn."""
    primary = _RecordingAdapter("primary", raise_exc=ConnectionError("upstream 503"))
    secondary = _RecordingAdapter("secondary", text=_structured("must-not-run"))
    fb = FallbackAdapter(
        [(primary, _opts("primary-model")), (secondary, _opts("secondary-model"))],
        transient_retries=0,
        transient_backoff_ms=0,
    )

    summary = asyncio.run(
        compact_history(
            adapter=fb,
            options=_opts("primary-model"),
            history_to_summarize=_HISTORY,
        )
    )

    assert summary == ""
    assert primary.calls == 1
    assert secondary.calls == 0


def test_non_fallback_adapter_path_unchanged() -> None:
    """Plain adapter passes through to the existing summarizer flow."""
    plain = _RecordingAdapter("plain", text=_structured("plain-summary"))
    summary = asyncio.run(
        compact_history(
            adapter=plain,
            options=_opts("plain-model"),
            history_to_summarize=_HISTORY,
        )
    )
    assert "plain-summary" in summary
    assert plain.calls == 1


def test_compaction_uses_primary_options_not_caller_supplied() -> None:
    """If a `FallbackAdapter` is given, the *primary's* options must be
    used regardless of what the caller passed in. Otherwise a stale
    chain-level options blob (e.g. the `last_used_options` from a prior
    failover turn) could mis-tag the compaction call.
    """
    primary = _RecordingAdapter("primary", text="ok")
    secondary = _RecordingAdapter("secondary")
    fb = FallbackAdapter(
        [(primary, _opts("primary-model")), (secondary, _opts("secondary-model"))],
        transient_retries=0,
        transient_backoff_ms=0,
    )
    # Caller passes a stale options (e.g. tagging secondary as last-used).
    asyncio.run(
        compact_history(
            adapter=fb,
            options=_opts("secondary-model"),
            history_to_summarize=_HISTORY,
        )
    )
    assert primary.calls == 1
    assert secondary.calls == 0
