"""A2 — compaction breaks observer turn delivery.

Codex Finding #4 + Claude coder C-2. `ChatSession.compact()` rewrites
`self.history` but does not update `_observer_last_index`. After
compaction, future calls to `_notify_observer_turn_end` slice
`history[stale_index:]` which is either empty (no new turns delivered)
or misaligned.

The repro uses a fake adapter so we don't need a live model.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter, StreamChunk


_STRUCTURED_REPLY = (
    "## Operator goals\n- legacy compact test\n"
    "## Decisions made\n- preserve observer-index invariant\n"
    "## Files touched\n- chat.py\n"
    "## Facts learned\n- compaction resets _observer_last_index\n"
    "## Open threads\n- none\n"
)


class _FakeAdapter(ModelAdapter):
    def __init__(self, compact_reply: str = _STRUCTURED_REPLY) -> None:
        # CR-0 M6: the post-compaction validator requires the 5 named
        # sections. Default reply is now a valid structured summary so
        # legacy tests keep exercising the rewrite path; supply your
        # own reply to test the malformed-output fallback explicitly.
        self._compact_reply = compact_reply

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        yield StreamChunk(type=ChunkType.TEXT, text=self._compact_reply)
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn", raw={"usage": {}})

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for m in messages:
            c = m.get("content", "")
            if isinstance(c, str):
                total += len(c) // 4
        return total

    async def check_available(self) -> bool:
        return True


async def test_a2_compaction_index() -> None:
    cs = ChatSession(
        adapter=_FakeAdapter(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=400_000),
        keep_recent_turns=4,
        # CR-0 (2026-05-22): legacy `[summary, *keep_recent]` shape requires
        # `head_anchor_messages=0` — the new default of 3 preserves the
        # first three user msgs verbatim. This test predates the head-anchor
        # work and pins the original shape on purpose; the observer-index
        # invariant being checked is orthogonal to the anchor count.
        head_anchor_messages=0,
    )
    # 30 fake user+assistant turns so compact() has something to fold.
    for i in range(15):
        cs.history.append({"role": "user", "content": f"u{i} " * 50})
        cs.history.append({"role": "assistant", "content": f"a{i} " * 50})

    # Simulate observer watermark near the end of history BEFORE compact.
    cs._observer_last_index = len(cs.history) - 2
    assert cs._observer_last_index == 28

    # compact() rewrites history to [summary, *keep_recent_turns].
    before, after = await cs.compact()
    assert before > 0 and after > 0
    new_len = len(cs.history)
    # keep_recent_turns=4 + 1 summary msg == 5
    assert new_len == 5, f"compact shape wrong: {new_len} != 5"

    # AFTER FIX: _observer_last_index must be clamped to within new history
    # (and ideally at len(history) so the compaction itself isn't re-observed).
    assert cs._observer_last_index <= new_len, (
        f"BUG: _observer_last_index={cs._observer_last_index} > history len {new_len}; "
        "next _notify_observer_turn_end will slice history[28:] — empty forever."
    )

    # Now simulate a new turn and confirm the observer-notify path sees it.
    cs.history.append({"role": "user", "content": "new_user_turn"})
    cs.history.append({"role": "assistant", "content": "new_assistant_turn"})

    start = cs._observer_last_index
    delta = cs.history[start:]
    chat_delta = [m for m in delta if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip()]
    assert len(chat_delta) >= 2, (
        f"BUG: post-compact delta has {len(chat_delta)} turns; observer sees nothing new"
    )
