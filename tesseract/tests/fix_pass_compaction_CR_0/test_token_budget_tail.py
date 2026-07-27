"""CR-0: the active window is sized by tokens, not message count.

Tool messages are 40%+ of a session's volume; counting messages
under-sizes the conversational tail. Operator-locked decision (#1):
"Active window is token-budgeted, not turn-counted."

Backwards-compat: when `active_window_tokens` is None, fall back to
`keep_recent_turns` (existing behavior).
"""

from __future__ import annotations

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import AdapterOptions

from .conftest import FakeAdapter, make_assistant, make_user


@pytest.mark.asyncio
async def test_token_budget_tail_keeps_more_than_keep_recent_turns_would() -> None:
    """A 5k-token budget should retain more verbatim history than the
    legacy 10-message tail when messages are small."""
    cs = ChatSession(
        adapter=FakeAdapter(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        head_anchor_messages=1,
        active_window_tokens=5_000,
    )
    cs.history.append(make_user("INTENT"))
    cs.history.append(make_assistant("ack"))
    # 60 small messages — fewer than 5k tokens total
    for i in range(60):
        cs.history.append(make_user(f"u{i} " * 5))
        cs.history.append(make_assistant(f"a{i} " * 5))

    # Force compaction by tightening threshold to 1% so should_compact fires
    cs.compact_threshold = 0.01
    if cs.should_compact():
        await cs.compact()

    user_msgs = [
        m for m in cs.history if m.get("role") == "user"
    ]
    assert len(user_msgs) > 12, (
        f"token-budget tail should retain more than the legacy 10-msg "
        f"window when content is small; kept {len(user_msgs)} user msgs"
    )


@pytest.mark.asyncio
async def test_token_budget_tail_trims_large_messages_more_aggressively() -> None:
    """Same message count, larger payloads → smaller tail."""
    cs = ChatSession(
        adapter=FakeAdapter(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        head_anchor_messages=1,
        active_window_tokens=5_000,
    )
    cs.history.append(make_user("INTENT"))
    cs.history.append(make_assistant("ack"))
    for i in range(60):
        cs.history.append(make_user(f"big-u-{i} " * 500))
        cs.history.append(make_assistant(f"big-a-{i} " * 500))

    cs.compact_threshold = 0.01
    await cs.compact()

    # Tail content should fit within ~5k tokens. With FakeAdapter
    # token estimate of ~1 token per 4 chars, large messages drop most.
    tail = [
        m for m in cs.history
        if m.get("role") in ("user", "assistant")
        and not (
            isinstance(m.get("content"), str)
            and m["content"].startswith("[Context from earlier in this session]")
        )
    ]
    tail_after_anchor = [
        m for m in tail
        if not (m.get("role") == "user" and m.get("content") == "INTENT")
    ]
    tail_tokens = cs.adapter.count_tokens(tail_after_anchor)
    assert tail_tokens <= 5_000 * 1.1, (
        f"tail tokens {tail_tokens} should be within ~10% of budget 5000"
    )


@pytest.mark.asyncio
async def test_legacy_keep_recent_turns_still_works_when_token_budget_unset() -> None:
    """Back-compat: existing callers that set `keep_recent_turns` and
    leave `active_window_tokens=None` get the legacy behavior — last N
    messages verbatim."""
    cs = ChatSession(
        adapter=FakeAdapter(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        head_anchor_messages=0,
        keep_recent_turns=6,
        active_window_tokens=None,
    )
    for i in range(20):
        cs.history.append(make_user(f"u{i} " * 50))
        cs.history.append(make_assistant(f"a{i} " * 50))

    cs.compact_threshold = 0.01
    await cs.compact()

    non_summary = [
        m for m in cs.history
        if not (
            isinstance(m.get("content"), str)
            and m["content"].startswith("[Context from earlier in this session]")
        )
    ]
    assert len(non_summary) == 6, (
        f"legacy keep_recent_turns=6 should yield 6 verbatim msgs; got {len(non_summary)}"
    )
