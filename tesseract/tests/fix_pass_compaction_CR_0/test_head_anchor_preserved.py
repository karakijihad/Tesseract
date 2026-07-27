"""CR-0: the first N user messages survive every compaction verbatim.

The head anchor is the StreamingLLM-style attention sink that prevents
"didn't recall what the chat was about" after the head of history is
folded into a summary. Operator-locked decision (Docs/Plan/context-recall/
INDEX.md §Operator-locked decisions #2): "Head anchor is permanent."
"""

from __future__ import annotations

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import AdapterOptions

from .conftest import FakeAdapter, make_assistant, make_user


@pytest.mark.asyncio
async def test_first_three_user_messages_survive_compaction() -> None:
    cs = ChatSession(
        adapter=FakeAdapter(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        head_anchor_messages=3,
        active_window_tokens=2_000,
    )
    # Three intent-setting user turns up front
    cs.history.append(make_user("INTENT-1: explore hermes vs tars"))
    cs.history.append(make_assistant("Acknowledged. Starting research."))
    cs.history.append(make_user("INTENT-2: focus on memory + retrieval"))
    cs.history.append(make_assistant("Reading the source files now."))
    cs.history.append(make_user("INTENT-3: include workshop indexing"))
    cs.history.append(make_assistant("Adding it to scope."))
    # Lots of middle content that should be compacted
    for i in range(30):
        cs.history.append(make_user(f"mid-user-{i} " * 100))
        cs.history.append(make_assistant(f"mid-assistant-{i} " * 100))

    before, after = await cs.compact()

    assert before > 0
    assert after < before, "compact should reduce token count"
    # The three intent-bearing user messages must appear verbatim at the
    # start of the rewritten history.
    user_msgs = [m for m in cs.history if m.get("role") == "user"]
    assert any("INTENT-1" in (m.get("content") or "") for m in user_msgs[:5])
    assert any("INTENT-2" in (m.get("content") or "") for m in user_msgs[:5])
    assert any("INTENT-3" in (m.get("content") or "") for m in user_msgs[:5])


@pytest.mark.asyncio
async def test_head_anchor_survives_two_compactions() -> None:
    """After back-to-back compactions, the original intent is still in
    the rewritten history. Today's `compact` would lose it because the
    head is folded into a paragraph summary and the summary itself is
    re-summarized on the next pass."""
    cs = ChatSession(
        adapter=FakeAdapter(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        head_anchor_messages=2,
        active_window_tokens=2_000,
    )
    cs.history.append(make_user("ORIG-INTENT: build the work-history index"))
    cs.history.append(make_assistant("OK starting."))
    cs.history.append(make_user("SECOND-INTENT: prefer SQLite FTS5"))
    cs.history.append(make_assistant("Mirror fts_index.py pattern."))
    for i in range(20):
        cs.history.append(make_user(f"slice-1-{i} " * 80))
        cs.history.append(make_assistant(f"slice-1-{i}-r " * 80))

    await cs.compact()

    # More middle content; second compaction
    for i in range(20):
        cs.history.append(make_user(f"slice-2-{i} " * 80))
        cs.history.append(make_assistant(f"slice-2-{i}-r " * 80))

    await cs.compact()

    user_texts = [
        (m.get("content") or "") for m in cs.history if m.get("role") == "user"
    ]
    flat = "\n".join(user_texts)
    assert "ORIG-INTENT" in flat, (
        "head anchor must survive iterative compaction"
    )
    assert "SECOND-INTENT" in flat, (
        "second anchor message must also survive"
    )


@pytest.mark.asyncio
async def test_head_anchor_count_configurable() -> None:
    cs = ChatSession(
        adapter=FakeAdapter(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        head_anchor_messages=1,
        active_window_tokens=2_000,
    )
    cs.history.append(make_user("KEEP-THIS"))
    cs.history.append(make_assistant("ack"))
    cs.history.append(make_user("ALSO-EARLY"))
    cs.history.append(make_assistant("ack"))
    for i in range(20):
        cs.history.append(make_user(f"u{i} " * 80))
        cs.history.append(make_assistant(f"a{i} " * 80))

    await cs.compact()

    user_texts = [
        (m.get("content") or "") for m in cs.history if m.get("role") == "user"
    ]
    flat = "\n".join(user_texts)
    # head_anchor=1 keeps the first user message; the second early message
    # may or may not appear in the tail depending on token budget, but
    # KEEP-THIS must survive as the anchor.
    assert "KEEP-THIS" in flat
