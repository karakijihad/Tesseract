"""Phase 5 Task 3 — terminal observation reaches the operator ONLY via the
existing suggestion etiquette (`ingest_memory_suggestion` /
`_pending_suggestions` / `_drain_pending_suggestions`), end to end.

Per the brief: the PTY push already reaches the same summarizer as chat
turns (`Observer._compose_system_prompt` renders `{pty_context}` from
`self._transcript.pty_buffer` on every call, including ones triggered by
a plain chat turn) — this is a TEST proving the existing funnel, not new
plumbing. Uses the REAL `agents/observer.md` definition (not a stubbed
`get_section`) so the assertion is against the live prompt template, not
an assumption about it.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator
from unittest.mock import MagicMock

from tesseract.agents.loader import load_agent
from tesseract.brain.chat import ChatSession
from tesseract.brain.memory_suggestion import MemorySuggestion
from tesseract.brain.observer import Observer, ObserverConfig
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter, StreamChunk

_SUGGESTION_JSON = (
    '{"kind": "remember", '
    '"target": {"kind": "quote", "turn_index": 0, '
    '"text": "npm run build failed with exit code 1"}, '
    '"reason": "operator terminal build failed and needs a look", '
    '"confidence": 0.82, "observation_id": "obs_test_terminal_funnel"}'
)


class _FakeAdapter(ModelAdapter):
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.last_messages: list[dict[str, Any]] | None = None

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls += 1
        self.last_messages = messages
        yield StreamChunk(type=ChunkType.TEXT, text=self.text)
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end", raw={"usage": {}})

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _build_observer(adapter: ModelAdapter) -> Observer:
    config = ObserverConfig(
        model="fake", provider="fake", temperature=1.0,
        max_output_tokens=512, context_window=32_000,
        timeout_seconds=30, max_retries=0,
    )
    agent_def = load_agent("observer")
    return Observer(adapter=adapter, config=config, agent_def=agent_def, cost_ledger=None)


async def test_pty_only_push_never_calls_the_llm() -> None:
    """A pane push with no chat activity enriches context but must not
    fire an adapter call by itself (`observe_incremental` docstring:
    "PTY-only feeds enrich context but do not trigger an LLM call")."""
    adapter = _FakeAdapter(_SUGGESTION_JSON)
    observer = _build_observer(adapter)

    result = await observer.observe_incremental([
        {"role": "pty", "pane_id": "p1", "text": "npm run build\nnpm run build failed with exit code 1\n", "timestamp": "t"},
    ])

    assert result is None
    assert adapter.calls == 0
    assert observer._transcript.pty_buffer, "pty push did not enrich the transcript"


async def test_pty_content_reaches_the_suggestion_prompt_on_next_chat_turn() -> None:
    """The next real chat turn's observe_incremental call renders the
    buffered PTY content into the system prompt via `{pty_context}` —
    proving terminal output reaches the same summarizer chat turns do."""
    adapter = _FakeAdapter(_SUGGESTION_JSON)
    observer = _build_observer(adapter)

    await observer.observe_incremental([
        {"role": "pty", "pane_id": "p1", "text": "npm run build\nnpm run build failed with exit code 1\n", "timestamp": "t"},
    ])

    suggestion = await observer.observe_incremental([
        {"role": "user", "content": "what happened in the terminal?"},
        {"role": "assistant", "content": "let me check"},
    ])

    assert adapter.last_messages is not None
    system_prompt = adapter.last_messages[0]["content"]
    assert "npm run build failed with exit code 1" in system_prompt, (
        "buffered PTY output never reached the suggestion-prompt system message"
    )

    assert isinstance(suggestion, MemorySuggestion)
    assert suggestion.kind == "remember"
    assert suggestion.observation_id == "obs_test_terminal_funnel"


async def test_suggestion_reaches_next_turn_injection_via_existing_funnel() -> None:
    """Full funnel: PTY-sourced suggestion -> ingest_memory_suggestion ->
    _pending_suggestions -> _drain_pending_suggestions (the one-shot
    injection ChatSession.send() prepends to the next turn). No new
    channel, no terminal-pane plumbing — the existing etiquette only."""
    adapter = _FakeAdapter(_SUGGESTION_JSON)
    observer = _build_observer(adapter)

    await observer.observe_incremental([
        {"role": "pty", "pane_id": "p1", "text": "npm run build failed with exit code 1\n", "timestamp": "t"},
    ])
    suggestion = await observer.observe_incremental([
        {"role": "user", "content": "what happened in the terminal?"},
        {"role": "assistant", "content": "let me check"},
    ])
    assert suggestion is not None

    chat = ChatSession(
        adapter=MagicMock(),
        system_prompt="",
        max_tool_iterations=10,
        max_consecutive_adapter_errors=3,
    )
    accepted = chat.ingest_memory_suggestion(suggestion)
    assert accepted is True
    assert len(chat._pending_suggestions) == 1

    injected = chat._drain_pending_suggestions()
    assert "[observer_suggestion]" in injected
    assert "npm run build failed with exit code 1" in injected
    assert "operator terminal build failed and needs a look" in injected
    # One-shot: drained queue is empty after the pop.
    assert len(chat._pending_suggestions) == 0

    # Re-ingesting the same observation_id is deduped (one-shot per suggestion).
    assert chat.ingest_memory_suggestion(suggestion) is False
