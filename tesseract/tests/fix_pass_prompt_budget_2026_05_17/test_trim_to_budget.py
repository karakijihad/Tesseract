"""2026-05-17 — prompt budget enforcement at the assembly chokepoint.

On the same day, a Telegram chat fired a prompt of 1,882,084
chars to codex (cap: 1,048,576). All six chain entries exhausted
because each fallback adapter received the same oversized payload.

`_trim_to_budget` is a backstop trim applied once in
`ChatSession._messages_for_turn` so codex / gpt-5.5 / nano / gemini
all receive a payload that fits. Drop order:
  1. Oldest history (preserve system + last KEEP_LAST_TURNS).
  2. Shrink `<recall_context>` in the latest user message.
  3. Never drop the latest user message itself.
"""

from __future__ import annotations

from tesseract.brain.chat import (
    KEEP_LAST_TURNS,
    PROMPT_CHAR_BUDGET,
    RECALL_CONTEXT_MIN_KEEP,
    _shrink_recall_block,
    _trim_to_budget,
)


def _msg(role: str, body: str) -> dict[str, str]:
    return {"role": role, "content": body}


def test_no_trim_when_under_budget() -> None:
    msgs = [_msg("system", "p"), _msg("user", "hi"), _msg("assistant", "hello")]
    out = _trim_to_budget(msgs, char_limit=10_000)
    assert out == msgs


def test_drops_oldest_history_first() -> None:
    big = "x" * 200_000
    msgs = [
        _msg("system", "sys"),
        _msg("user", big),  # oldest — should be dropped
        _msg("assistant", big),
        _msg("user", big),
        _msg("assistant", big),
        _msg("user", "current inbound"),
    ]
    out = _trim_to_budget(msgs, char_limit=500_000)
    roles = [m["role"] for m in out]
    # System + marker + last 3 + current user (or some subset).
    assert roles[0] == "system"
    assert out[-1]["content"] == "current inbound"
    # The first big oldest pair should be gone.
    assert sum(1 for m in out if m.get("content") == big) < 4


def test_preserves_system_and_keep_last_turns() -> None:
    big = "x" * 200_000
    msgs = [_msg("system", "p")] + [_msg("user", big) for _ in range(10)] + [_msg("user", "now")]
    out = _trim_to_budget(msgs, char_limit=500_000)
    assert out[0] == {"role": "system", "content": "p"}
    assert out[-1] == {"role": "user", "content": "now"}
    # Last KEEP_LAST_TURNS history items survive.
    assert sum(1 for m in out[-KEEP_LAST_TURNS:]) == KEEP_LAST_TURNS


def test_shrinks_recall_context_in_latest_user_when_still_over() -> None:
    big_recall = "y" * 400_000
    user_body = f"<recall_context>\n{big_recall}\n</recall_context>\n\nhelp me"
    msgs = [_msg("system", "p"), _msg("user", user_body)]
    out = _trim_to_budget(msgs, char_limit=50_000)
    total = sum(len(m["content"]) for m in out)
    assert total <= 50_000
    assert "help me" in out[-1]["content"]


def test_shrink_recall_below_min_keep_drops_block() -> None:
    recall_inner = "z" * 50_000
    body = f"head\n<recall_context>\n{recall_inner}\n</recall_context>\ntail"
    out = _shrink_recall_block(body, target_len=RECALL_CONTEXT_MIN_KEEP - 100)
    assert "<recall_context>" not in out
    assert "head" in out and "tail" in out


def test_latest_user_message_never_dropped() -> None:
    big = "x" * 2_000_000
    msgs = [_msg("system", "p"), _msg("user", big)]
    out = _trim_to_budget(msgs, char_limit=500_000)
    # Latest message preserved even when alone-and-too-big — adapter will
    # reject, but the operator's actual question is not silently deleted.
    assert out[-1]["role"] == "user"
    assert out[-1]["content"] == big


def test_budget_constant_under_codex_cap() -> None:
    # Codex CLI rejects at 1_048_576 chars. Need headroom for adapter
    # wrapping + output tokens.
    assert PROMPT_CHAR_BUDGET < 1_048_576
    assert PROMPT_CHAR_BUDGET >= 800_000
