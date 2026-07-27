"""MP-2 — when ChatSession.send is called with `view_snapshot`, the
chat brain renders a `[current_view] <id>` + `[view_state] <json>`
block as a one-shot user-side injection on iteration 0 of the turn.

The block must:
- appear in the messages passed to the adapter
- be cleared after the first iteration (one-shot, not persistent)
- never reach the adapter when `view_snapshot=None`
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


class _RecordingAdapter(ModelAdapter):
    """Captures the message list the adapter receives so the test can
    assert the view-snapshot block landed in the right place."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls.append([dict(m) for m in messages])
        yield StreamChunk(type=ChunkType.TEXT, text="ok")
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn", raw={"usage": {}})

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return sum(len(str(m.get("content", ""))) for m in messages) // 4

    async def check_available(self) -> bool:
        return True


def _make_session(adapter: ModelAdapter) -> ChatSession:
    return ChatSession(
        adapter=adapter,
        system_prompt="you are tars",
        max_tool_iterations=4,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=400_000),
    )


def _injection_text(messages: list[dict[str, Any]]) -> str:
    """The view-snapshot injection lands as a user-role message that is
    NOT the final history entry (it's anchored immediately before the
    real user turn). Concatenate every user-role content into one
    string so the test can substring-match without caring which slot."""
    return "\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "user"
    )


@pytest.mark.asyncio
async def test_view_snapshot_lands_in_iteration_zero_messages() -> None:
    adapter = _RecordingAdapter()
    cs = _make_session(adapter)
    snapshot = {
        "view": "missions",
        "view_state": {"selected_mission_id": "m_abc123", "focused_step_id": None},
    }
    async for _ in cs.send("help me with this row", view_snapshot=snapshot):
        pass

    assert adapter.calls, "adapter should have been called at least once"
    text = _injection_text(adapter.calls[0])
    assert "[current_view] missions" in text
    assert "[view_state]" in text
    assert "m_abc123" in text


@pytest.mark.asyncio
async def test_view_snapshot_is_one_shot_not_persistent() -> None:
    """A second turn without an explicit snapshot must NOT carry the
    previous turn's `[current_view]` block — `_turn_injection` is
    cleared between turns."""
    adapter = _RecordingAdapter()
    cs = _make_session(adapter)
    snapshot = {"view": "settings", "view_state": {"open_sections": ["model-roles"]}}
    async for _ in cs.send("first", view_snapshot=snapshot):
        pass
    async for _ in cs.send("second"):
        pass

    second_call = adapter.calls[-1]
    text = _injection_text(second_call)
    assert "[current_view]" not in text, (
        "second-turn prompt should not carry first-turn snapshot"
    )


@pytest.mark.asyncio
async def test_no_snapshot_means_no_block() -> None:
    """The control case — `view_snapshot=None` (or absent) leaves the
    prompt unchanged. Without this counter-test a buggy fallthrough
    could inject an empty `[current_view]` block on every turn."""
    adapter = _RecordingAdapter()
    cs = _make_session(adapter)
    async for _ in cs.send("plain message"):
        pass

    text = _injection_text(adapter.calls[0])
    assert "[current_view]" not in text


@pytest.mark.asyncio
async def test_empty_view_snapshot_dict_is_skipped() -> None:
    """Defensive: a snapshot envelope whose `view` is empty string is
    treated as no snapshot — never emit a header with a blank id."""
    adapter = _RecordingAdapter()
    cs = _make_session(adapter)
    async for _ in cs.send("plain", view_snapshot={"view": "", "view_state": {}}):
        pass

    text = _injection_text(adapter.calls[0])
    assert "[current_view]" not in text
