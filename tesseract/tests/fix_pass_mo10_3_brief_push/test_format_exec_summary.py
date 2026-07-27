"""MO-10-3 §2b — format_exec_summary across populated / empty / oversize inputs."""

from __future__ import annotations

from tesseract.integrations.telegram.brief_push import (
    HARD_LIMIT_CHARS,
    format_exec_summary,
)


def _full_payload() -> dict:
    return {
        "kind": "daily_brief",
        "date": "2026-05-15",
        "sections": {
            "yesterday_in_tesseract": "TARS shipped MO-10-1. Tree green, tests green.",
            "yesterday_with_you": "You merged MO-9-14 and started MO-10-2 scaffolding.",
            "what_i_learned": "Content-merge needs three-way diffs to survive operator edits.",
            "vault": [{"title": "Three-way merge paper notes"}],
            "world": {
                "tech": [{"title": "Anthropic ships Claude 4.8 family"}],
                "science": [{"title": "Quantum error correction milestone"}],
                "politics": [{"title": "EU AI Act enforcement update"}],
            },
        },
    }


def test_format_full_returns_all_sections():
    text = format_exec_summary(_full_payload())
    assert "TESSERACT — 2026-05-15" in text
    assert "Yesterday in TESSERACT" in text
    assert "With you" in text
    assert "What I learned" in text
    assert "Vault" in text
    assert "World" in text
    assert "<b>Tech:</b>" in text
    assert "<b>Science:</b>" in text
    assert "<b>Politics:</b>" in text


def test_format_drops_empty_sections():
    payload = _full_payload()
    payload["sections"]["vault"] = []
    payload["sections"]["world"] = {"tech": [], "science": [], "politics": []}
    text = format_exec_summary(payload)
    assert "Vault" not in text
    assert "World" not in text
    # Other sections still present.
    assert "Yesterday in TESSERACT" in text


def test_format_truncates_over_limit():
    payload = _full_payload()
    huge = "TARS shipped MO-10-1. " + ("Lorem ipsum dolor sit amet " * 100)
    payload["sections"]["yesterday_in_tesseract"] = huge
    text = format_exec_summary(payload)
    # Voice-prose sections are clipped per-section, and then we still
    # hard-truncate the whole message at HARD_LIMIT_CHARS.
    payload["sections"]["yesterday_with_you"] = huge
    payload["sections"]["what_i_learned"] = huge
    text = format_exec_summary(payload)
    assert len(text) <= HARD_LIMIT_CHARS


def test_format_html_escapes_dangerous_chars():
    payload = _full_payload()
    payload["sections"]["yesterday_in_tesseract"] = "<script>alert('xss')</script>"
    text = format_exec_summary(payload)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text or "alert" not in text  # markdown_to_telegram_html escapes


def test_format_empty_payload_returns_empty():
    assert format_exec_summary(None) == ""
    assert format_exec_summary({}) == ""
    assert format_exec_summary({"sections": "not a dict"}) == ""
