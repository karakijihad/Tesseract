"""Owner batch 1 follow-up — `FallbackAdapter` must surface fallback
events so the operator can see when the primary failed and a secondary
answered.

Before the fix, MODEL_SELECTED was emitted once at the start of the turn
with the primary's options. If the primary failed pre-commit and a
fallback committed, the chat bubble's badge stayed pinned to the
primary's model name, hiding the fact that a different model produced
the response.

The fix: when FallbackAdapter commits on a non-primary entry, emit a
fresh MODEL_SELECTED chunk with `is_fallback=True`, the actual entry's
options, the primary's identity, and the failure reason — so the
frontend ModelBadge swaps + the pulse / toast log the event.
"""

from __future__ import annotations

from typing import Any

import pytest

from tesseract.brain.adapter_chain import FallbackAdapter
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, StreamChunk


class _StubAdapter:
    def __init__(self, model: str, mode: str) -> None:
        self.model = model
        self._mode = mode

    async def stream(self, *, messages, tools, options):
        if self._mode == "pre_commit_error":
            yield StreamChunk(type=ChunkType.ERROR, error=f"{self.model} pre-commit failed")
            return
        if self._mode == "ok_text":
            yield StreamChunk(type=ChunkType.TEXT, text=f"hello from {self.model}")
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end")
            return
        raise RuntimeError(f"unknown stub mode {self._mode}")

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _opts(model: str, provider: str, effort: str = "high") -> AdapterOptions:
    return AdapterOptions(
        role="chat_brain",
        provider=provider,
        model=model,
        reasoning_effort=effort,
        tier="api",
    )


@pytest.mark.asyncio
async def test_primary_success_does_not_emit_fallback_model_selected():
    """Primary commits cleanly — no extra MODEL_SELECTED is injected."""
    chain = [
        (_StubAdapter("gpt-5.4-nano", "ok_text"), _opts("gpt-5.4-nano", "openai")),
        (_StubAdapter("gpt-5.4-mini", "ok_text"), _opts("gpt-5.4-mini", "openai")),
    ]
    fa = FallbackAdapter(chain, transient_retries=0, transient_backoff_ms=0)
    chunks = [c async for c in fa.stream(messages=[], tools=None, options=None)]
    selects = [c for c in chunks if c.type == ChunkType.MODEL_SELECTED]
    assert selects == []  # primary committed; no fallback notification


@pytest.mark.asyncio
async def test_fallback_emits_model_selected_with_primary_context():
    """Primary fails pre-commit, fallback commits — a MODEL_SELECTED chunk
    with `is_fallback=True` must precede the first committed chunk and
    carry the actual entry's options + the primary's identity + reason."""
    chain = [
        (_StubAdapter("gpt-5.4-nano", "pre_commit_error"), _opts("gpt-5.4-nano", "openai")),
        (_StubAdapter("gpt-5.4-mini", "ok_text"), _opts("gpt-5.4-mini", "openai")),
    ]
    fa = FallbackAdapter(chain, transient_retries=0, transient_backoff_ms=0)
    chunks = [c async for c in fa.stream(messages=[], tools=None, options=None)]

    selects = [c for c in chunks if c.type == ChunkType.MODEL_SELECTED]
    assert len(selects) == 1, f"expected exactly one fallback MODEL_SELECTED, got {len(selects)}"
    raw = selects[0].raw or {}
    assert raw.get("is_fallback") is True
    assert raw.get("model") == "gpt-5.4-mini"
    assert raw.get("provider") == "openai"
    assert raw.get("chain_index") == 1
    assert raw.get("primary", {}).get("model") == "gpt-5.4-nano"
    assert "pre-commit failed" in (raw.get("fallback_reason") or "").lower()

    # The MODEL_SELECTED must precede the first TEXT chunk so the
    # frontend bubble's ModelBadge updates BEFORE the user sees text.
    first_select_idx = next(i for i, c in enumerate(chunks) if c.type == ChunkType.MODEL_SELECTED)
    first_text_idx = next(i for i, c in enumerate(chunks) if c.type == ChunkType.TEXT)
    assert first_select_idx < first_text_idx


@pytest.mark.asyncio
async def test_fallback_emits_model_selected_only_once_even_on_streaming():
    """If the fallback streams multiple TEXT chunks, only one
    MODEL_SELECTED is injected (on first commit)."""
    class _MultiText(_StubAdapter):
        async def stream(self, *, messages, tools, options):
            yield StreamChunk(type=ChunkType.TEXT, text="first ")
            yield StreamChunk(type=ChunkType.TEXT, text="second ")
            yield StreamChunk(type=ChunkType.TEXT, text="third")
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end")

    chain = [
        (_StubAdapter("gpt-5.4-nano", "pre_commit_error"), _opts("gpt-5.4-nano", "openai")),
        (_MultiText("gpt-5.4-mini", "ok_text"), _opts("gpt-5.4-mini", "openai")),
    ]
    fa = FallbackAdapter(chain, transient_retries=0, transient_backoff_ms=0)
    chunks = [c async for c in fa.stream(messages=[], tools=None, options=None)]

    selects = [c for c in chunks if c.type == ChunkType.MODEL_SELECTED]
    texts = [c for c in chunks if c.type == ChunkType.TEXT]
    assert len(selects) == 1
    assert len(texts) == 3
