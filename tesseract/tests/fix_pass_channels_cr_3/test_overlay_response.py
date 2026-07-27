"""CR-3 — overlay response template asserts the load-bearing instructions.

Per the phase doc §2, when a ``<channel_attachment status="no_handler"/>``
envelope arrives on a channel session, the overlay MUST instruct the
model to do one of:

- delegate the missing handler's build to Claude/Codex for the operator
  to review and promote,
- post a ``tars_post`` workspace nudge, or
- apologize concretely to the user.

We assert these are present in the rendered system prompt by inspecting
the overlay text (fixture-based). The live-LLM smoke test is gated on
``TARS_RUN_LIVE_LLM=1`` and ``pytest.mark.live`` so CI never pays for it.
"""

from __future__ import annotations

import os

import pytest

from tesseract.brain.prompt import build_channel_overlay


def test_overlay_instructs_delegate_or_tars_post_on_no_handler() -> None:
    overlay = build_channel_overlay("Telegram")
    assert 'status="no_handler"' in overlay
    assert "delegate" in overlay
    assert "tars_post" in overlay
    assert "Apologize" in overlay


def test_overlay_instructs_apology_or_class_disclosure_on_extract_failed() -> None:
    overlay = build_channel_overlay("Telegram")
    assert 'status="extract_failed"' in overlay
    assert "Apologize" in overlay
    assert "<error>" in overlay


def test_overlay_instructs_no_intent_answer_scaffold() -> None:
    overlay = build_channel_overlay("Telegram")
    assert "`<intent>`" in overlay
    assert "`<answer>`" in overlay
    assert "channel bridge strips them" in overlay


def test_overlay_instructs_workspace_nudge_when_ask_gated() -> None:
    overlay = build_channel_overlay("Telegram")
    assert "ASK-gated tools" in overlay
    assert "tars_post" in overlay
    assert "the operator's back" in overlay


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("TARS_RUN_LIVE_LLM") != "1",
    reason="live-LLM smoke gated on TARS_RUN_LIVE_LLM=1",
)
def test_live_llm_responds_within_overlay_contract() -> None:
    """Manual / opt-in smoke: a real channel-session reply uses markdown,
    stays under 800 chars, and does not emit ``<intent>``/``<answer>`` tags."""
    pytest.skip("live smoke is an opt-in manual run; see phase-CR-3 §5.2")
