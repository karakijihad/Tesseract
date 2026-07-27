"""Chat-turn promise audit (codex audit-1/2 follow-up #7).

Built after the 2026-05-19 Telegram confabulation incident: TARS said
"Done. Every 15 minutes I'll fire a toast" with no schedule_update
tool invocation. The model bump (gpt54_mini → gpt_oss_120b) is the
primary defense; this regex is a backstop that logs WARNING when an
assistant text claims an action but the turn invoked no tools.

The pattern intentionally targets *terminal* action claims — phrases
that imply an external state change just happened. Conversational
phrases ("Done analyzing X", "I have read the file") must not match.
"""

from __future__ import annotations

import pytest

from tesseract.brain.chat import _PROMISE_REGEX, _audit_promise_without_action


# -- Positive cases (should fire the audit) --------------------------------


@pytest.mark.parametrize("text", [
    "Done. Every 15 minutes I will fire a toast with a brief status summary.",
    "All set. You will get a concise health-check toast every 15 minutes.",
    "I have created the schedule.",
    "I have scheduled the operator nudge.",
    "I have already enabled the job.",
    "I have set up the job for you.",
    "I've added it.",
    "I'm already configured.",
    "Scheduled.",
    "Enabled.",
    "Configured.",
    "All set!",
    "Done!",
    "Done—every 15 min you'll get a toast.",
])
def test_promise_regex_matches_action_claim(text: str) -> None:
    match = _PROMISE_REGEX.search(text)
    assert match is not None, f"should have matched: {text!r}"


# -- Negative cases (false positives we explicitly DON'T want) -------------


@pytest.mark.parametrize("text", [
    "Done analyzing your code.",
    "Done with the analysis — here are the results.",
    "When I am done analyzing it I will share.",
    "I have read the file.",
    "I have looked at the schedule.yaml.",
    "Here is what I found.",
    "The job is already scheduled and you can verify by checking schedule.yaml.",
    "I'm going to schedule that for you — confirm first?",  # future tense
    "Should I enable it?",  # question, not claim
    "If you want, I can schedule it.",  # conditional offer
])
def test_promise_regex_skips_conversational(text: str) -> None:
    match = _PROMISE_REGEX.search(text)
    assert match is None, f"false positive: {text!r} -> {match.group(0) if match else None!r}"


# -- _audit_promise_without_action helper behaviour ------------------------


class _StubOptions:
    role = "chat_brain"
    provider = "nim"
    model = "gpt-oss-120b"


def test_audit_logs_warning_when_promise_without_tool(caplog) -> None:
    """The 2026-05-19 incident: model claimed "Done" with zero tool
    invocations. Audit must log a WARNING so the operator can spot it."""
    caplog.set_level("WARNING")
    _audit_promise_without_action(
        assistant_text=[
            "Done. Every 15 minutes I'll fire a toast with a brief status summary."
        ],
        turn_tool_invocations=0,
        options=_StubOptions(),
    )
    assert any(
        "PROMISE_WITHOUT_ACTION" in rec.message
        for rec in caplog.records
    ), "audit should log a WARNING for promise without tool call"


def test_audit_silent_when_tools_were_invoked(caplog) -> None:
    """If the turn DID invoke a tool, even action-claim language is
    presumed honest — the tool result is the evidence."""
    caplog.set_level("WARNING")
    _audit_promise_without_action(
        assistant_text=["Done. Schedule updated."],
        turn_tool_invocations=1,  # one or more tools fired
        options=_StubOptions(),
    )
    assert not any(
        "PROMISE_WITHOUT_ACTION" in rec.message
        for rec in caplog.records
    )


def test_audit_silent_on_purely_conversational_reply(caplog) -> None:
    caplog.set_level("WARNING")
    _audit_promise_without_action(
        assistant_text=["Here are four listings I found. Which would you like to explore?"],
        turn_tool_invocations=0,
        options=_StubOptions(),
    )
    assert not any(
        "PROMISE_WITHOUT_ACTION" in rec.message
        for rec in caplog.records
    )


def test_audit_silent_on_empty_text(caplog) -> None:
    caplog.set_level("WARNING")
    _audit_promise_without_action(
        assistant_text=[],
        turn_tool_invocations=0,
        options=_StubOptions(),
    )
    assert not any(
        "PROMISE_WITHOUT_ACTION" in rec.message
        for rec in caplog.records
    )
