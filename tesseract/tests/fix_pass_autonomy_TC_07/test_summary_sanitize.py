"""Tests for clean_summary_tail — root-cause fix for broken "}" autonomy titles."""

from __future__ import annotations

from tesseract.orchestrator.autonomy.summary_sanitize import clean_summary_tail


def test_strips_trailing_fenced_json_block() -> None:
    raw = 'We should update the config.\n```json\n{"x":1}\n```'
    result = clean_summary_tail(raw, tail_chars=500)
    assert "```" not in result
    assert "{" not in result
    assert result.endswith("config.")


def test_strips_leading_fragment_debris() -> None:
    raw = "`). Move the tagged code"
    result = clean_summary_tail(raw, tail_chars=500)
    assert result.startswith("Move")


def test_only_punctuation_returns_empty() -> None:
    assert clean_summary_tail("}", tail_chars=500) == ""
    assert clean_summary_tail('"). ', tail_chars=500) == ""


def test_preserves_we_should_prefix() -> None:
    raw = "We should refactor X."
    result = clean_summary_tail(raw, tail_chars=500)
    assert result == "We should refactor X."


def test_preserves_please_prefix() -> None:
    raw = "please review the change."
    result = clean_summary_tail(raw, tail_chars=500)
    assert result == "please review the change."


def test_long_paragraph_returns_exact_tail_length() -> None:
    raw = " ".join(["word"] * 300)
    tail_chars = 50
    result = clean_summary_tail(raw, tail_chars=tail_chars)
    assert len(result) == tail_chars
    assert result == raw[-tail_chars:]


def test_empty_and_whitespace_input() -> None:
    assert clean_summary_tail("", tail_chars=500) == ""
    assert clean_summary_tail("   \n\t  ", tail_chars=500) == ""


def test_unterminated_trailing_fence_dropped() -> None:
    raw = "Summary text here.\n```python\ndef f():\n    pass"
    result = clean_summary_tail(raw, tail_chars=500)
    assert "```" not in result
    assert "def f" not in result
    assert result.endswith("here.")


def test_collapses_whitespace_runs() -> None:
    raw = "Hello   world\n\n\nagain."
    result = clean_summary_tail(raw, tail_chars=500)
    assert result == "Hello world again."


def test_debris_at_slice_boundary_is_recovered_not_dropped() -> None:
    """Regression: when raw output is longer than tail_chars, the slice
    boundary itself can land on punctuation debris (e.g. a stray '}' from
    truncated JSON) immediately followed by a real, actionable sentence.
    The debris must be stripped from the resulting tail, not treated as
    "only punctuation" and discarded wholesale."""
    suffix = "}. We need to add retry logic with backoff."
    raw = "x" * 500 + suffix
    result = clean_summary_tail(raw, tail_chars=len(suffix))
    assert result == "We need to add retry logic with backoff."
