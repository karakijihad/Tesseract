"""CR-0: a second compaction APPENDS a new slice block to the running
summary; it does not re-summarize prior slices.

Operator-locked decision (INDEX.md #3): "Compressed middle grows by
append, never by re-summarization." This prevents iterative
information loss (the 4-sentence→2-sentence→1-sentence rot we see
today).
"""

from __future__ import annotations

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import AdapterOptions

from .conftest import FakeAdapter, make_assistant, make_user


SLICE_1_REPLY = (
    "## Operator goals\n- design Phase A\n"
    "## Decisions made\n- chose sliding window\n"
    "## Files touched\n- chat.py\n"
    "## Facts learned\n- prefers structured summaries\n"
    "## Open threads\n- still need tests\n"
)

SLICE_2_REPLY = (
    "## Operator goals\n- finalize Phase B\n"
    "## Decisions made\n- token budget over turn count\n"
    "## Files touched\n- compaction.py\n"
    "## Facts learned\n- batch-workflow preferred\n"
    "## Open threads\n- still need code review\n"
)


@pytest.mark.asyncio
async def test_second_compaction_appends_does_not_rewrite() -> None:
    adapter = FakeAdapter(reply=SLICE_1_REPLY)
    cs = ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        head_anchor_messages=2,
        active_window_tokens=2_000,
    )
    cs.history.append(make_user("INTENT"))
    cs.history.append(make_assistant("ack"))
    cs.history.append(make_user("PLAN"))
    cs.history.append(make_assistant("planning"))
    for i in range(20):
        cs.history.append(make_user(f"a{i} " * 60))
        cs.history.append(make_assistant(f"b{i} " * 60))

    await cs.compact()

    # Find the running summary message after first compaction
    summary_msgs_after_1 = [
        m for m in cs.history
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m["content"].startswith("[Context from earlier in this session]")
    ]
    assert len(summary_msgs_after_1) == 1
    first_summary_text = summary_msgs_after_1[0]["content"]
    assert "design Phase A" in first_summary_text
    assert "chose sliding window" in first_summary_text

    # Second slice; switch adapter reply
    adapter.set_reply(SLICE_2_REPLY)
    for i in range(20):
        cs.history.append(make_user(f"c{i} " * 60))
        cs.history.append(make_assistant(f"d{i} " * 60))

    await cs.compact()

    summary_msgs_after_2 = [
        m for m in cs.history
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m["content"].startswith("[Context from earlier in this session]")
    ]
    assert len(summary_msgs_after_2) == 1, (
        "must still be exactly one running summary message"
    )
    second_summary_text = summary_msgs_after_2[0]["content"]

    # BOTH slices' content must be present — slice 1 verbatim, slice 2 appended.
    assert "design Phase A" in second_summary_text, (
        "prior slice content must be preserved verbatim across compactions"
    )
    assert "chose sliding window" in second_summary_text, (
        "prior decisions must not be rewritten"
    )
    assert "finalize Phase B" in second_summary_text, (
        "new slice content must be appended"
    )
    assert "token budget over turn count" in second_summary_text


@pytest.mark.asyncio
async def test_appended_slices_carry_dated_block_header() -> None:
    """Each appended slice gets a `# Slice` header so the model can
    reason about which decisions came from when."""
    adapter = FakeAdapter(reply=SLICE_1_REPLY)
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
    adapter.set_reply(SLICE_2_REPLY)
    for i in range(20):
        cs.history.append(make_user(f"v{i} " * 60))
        cs.history.append(make_assistant(f"b{i} " * 60))
    await cs.compact()

    summary_msg = next(
        m for m in cs.history
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m["content"].startswith("[Context from earlier in this session]")
    )
    text = summary_msg["content"]
    # Two slice blocks → two `# Slice` headers (H1).
    headers = [
        ln for ln in text.splitlines() if ln.startswith("# Slice")
    ]
    assert len(headers) == 2, f"expected 2 slice headers, got {len(headers)}: {headers}"
