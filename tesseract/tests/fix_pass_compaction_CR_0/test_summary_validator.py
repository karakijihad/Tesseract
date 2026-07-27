"""M6 follow-up to CR-0: reject malformed compaction summaries.

The adapter is prompted to emit 5 named ## sections. If it returns
something else (free-form prose, missing sections, wrong headers),
``compact_history`` returns an empty string and ``ChatSession.compact``
preserves the full history rather than fold it into garbage. Matches
the existing empty-summary fallback behavior.
"""

from __future__ import annotations

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.brain.compaction import _validate_structured_summary
from tesseract.kernel.adapters.base import AdapterOptions

from .conftest import FakeAdapter, make_assistant, make_user


_GOOD_REPLY = (
    "## Operator goals\n- design\n"
    "## Decisions made\n- chose A\n"
    "## Files touched\n- chat.py\n"
    "## Facts learned\n- structured beats prose\n"
    "## Open threads\n- need tests\n"
)


def test_validator_accepts_well_formed_summary() -> None:
    assert _validate_structured_summary(_GOOD_REPLY) is True


def test_validator_rejects_missing_section() -> None:
    bad = (
        "## Operator goals\n- a\n"
        "## Decisions made\n- b\n"
        "## Files touched\n- c\n"
        "## Facts learned\n- d\n"
        # Missing "## Open threads"
    )
    assert _validate_structured_summary(bad) is False


def test_validator_rejects_freeform_prose() -> None:
    bad = (
        "Earlier in the conversation, the operator discussed several "
        "things and we made some decisions. Now we should continue."
    )
    assert _validate_structured_summary(bad) is False


def test_validator_rejects_empty_string() -> None:
    assert _validate_structured_summary("") is False


def test_validator_rejects_only_some_headers() -> None:
    bad = "## Operator goals\n- a\n## Decisions made\n- b\n"
    assert _validate_structured_summary(bad) is False


@pytest.mark.asyncio
async def test_compact_keeps_full_history_when_summary_malformed() -> None:
    """End-to-end: ChatSession.compact() with a malformed adapter
    response must not corrupt history."""
    adapter = FakeAdapter(reply="garbage prose with no headers at all")
    cs = ChatSession(
        adapter=adapter,
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=100_000),
        head_anchor_messages=1,
        active_window_tokens=2_000,
    )
    cs.history.append(make_user("INTENT"))
    cs.history.append(make_assistant("ack"))
    for i in range(20):
        cs.history.append(make_user(f"u{i} " * 80))
        cs.history.append(make_assistant(f"a{i} " * 80))

    history_before = len(cs.history)
    history_snapshot = [m.get("content") for m in cs.history]

    before, after = await cs.compact()

    # No-op: counts equal, history unchanged.
    assert before == after, "malformed summary must short-circuit compact()"
    assert len(cs.history) == history_before, (
        "history length changed despite malformed summary"
    )
    assert [m.get("content") for m in cs.history] == history_snapshot, (
        "history content changed despite malformed summary"
    )
