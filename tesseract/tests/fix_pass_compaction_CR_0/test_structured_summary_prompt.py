"""CR-0: the compaction system prompt asks for STRUCTURED output —
the 5 named sections — not a 4-6 sentence paragraph.

Operator-locked decision: structured beats free-form because the
model can retrieve from named sections mid-conversation.
"""

from __future__ import annotations

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import AdapterOptions

from .conftest import FakeAdapter, make_assistant, make_user


_REQUIRED_SECTION_HEADERS = [
    "## Operator goals",
    "## Decisions made",
    "## Files touched",
    "## Facts learned",
    "## Open threads",
]


@pytest.mark.asyncio
async def test_compaction_system_prompt_requests_structured_sections() -> None:
    adapter = FakeAdapter()
    cs = ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        head_anchor_messages=1,
        active_window_tokens=1_500,
    )
    cs.history.append(make_user("INTENT"))
    cs.history.append(make_assistant("ack"))
    for i in range(20):
        cs.history.append(make_user(f"u{i} " * 60))
        cs.history.append(make_assistant(f"a{i} " * 60))

    await cs.compact()

    assert adapter.call_count == 1, "exactly one summarizer adapter call expected"
    system_prompt = adapter.last_system
    for header in _REQUIRED_SECTION_HEADERS:
        assert header in system_prompt, (
            f"compaction system prompt must instruct on section: {header!r}"
        )


@pytest.mark.asyncio
async def test_compaction_prompt_carries_prior_summary_for_append() -> None:
    """When a running summary already exists, the next compaction's
    system prompt must mention the append contract — the model is
    told NOT to re-summarize prior slices."""
    adapter = FakeAdapter()
    cs = ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        head_anchor_messages=1,
        active_window_tokens=1_200,
    )
    cs.history.append(make_user("INTENT"))
    cs.history.append(make_assistant("ack"))
    for i in range(20):
        cs.history.append(make_user(f"u{i} " * 60))
        cs.history.append(make_assistant(f"a{i} " * 60))

    await cs.compact()
    # New slice → second compaction with prior summary in history
    for i in range(20):
        cs.history.append(make_user(f"v{i} " * 60))
        cs.history.append(make_assistant(f"b{i} " * 60))
    await cs.compact()

    assert adapter.call_count == 2
    second_prompt = adapter.last_system
    second_prompt_lower = second_prompt.lower()
    # The append-mode prompt must tell the model not to re-summarize.
    assert "append" in second_prompt_lower or "new slice" in second_prompt_lower, (
        "append-mode compaction prompt must signal that the new output covers "
        "only the new slice, not the prior summary"
    )
