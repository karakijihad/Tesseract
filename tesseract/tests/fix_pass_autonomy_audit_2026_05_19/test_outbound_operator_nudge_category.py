"""Codex audit follow-up — new ``operator_nudge`` notification category.

Confirms the category is registered, NOT in EXEMPT_CATEGORIES (operator
can mute it from NotificationsPane), and renders a sensible body via
``format_message``.
"""

from __future__ import annotations

from tesseract.orchestrator.autonomy.outbound import (
    CATEGORIES,
    EXEMPT_CATEGORIES,
    format_message,
)


def test_operator_nudge_in_categories() -> None:
    assert "operator_nudge" in CATEGORIES


def test_operator_nudge_not_exempt() -> None:
    """Exempt categories bypass rate cap + mute (recovery_summary,
    crash_storm_latched, awaiting_operator). A chatty nudge must NOT
    join that club — operator should be able to silence it."""
    assert "operator_nudge" not in EXEMPT_CATEGORIES


def test_format_message_operator_nudge_with_text() -> None:
    body = format_message("operator_nudge", {"text": "All good. 3 agenda · 0 paused."})
    assert "All good" in body
    assert "nudge" in body.lower()


def test_format_message_operator_nudge_default_text() -> None:
    """Missing/empty ``text`` falls back to a sensible default rather
    than rendering as an empty string."""
    body = format_message("operator_nudge", {})
    assert body  # non-empty
    assert "good" in body.lower() or "healthy" in body.lower()


def test_format_message_operator_nudge_truncated() -> None:
    """``_truncate`` caps at MAX_MESSAGE_CHARS (512). Long text must
    not blow up the Telegram send."""
    long_text = "x" * 2000
    body = format_message("operator_nudge", {"text": long_text})
    assert len(body) <= 512
