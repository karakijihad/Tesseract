"""AU-15: pure reducer + transform unit tests."""
from __future__ import annotations

import pytest

from tesseract.kernel.tokenjuice.reducers import (
    REDUCERS,
    TRANSFORMS,
    apply_reducer,
    cap_chars,
    cap_lines,
    dedup_adjacent,
    dedup_lines,
    drop_regex,
    head_lines,
    head_tail,
    passthrough,
    pretty_json,
    strip_ansi,
    tail_lines,
    trim_empty_edges,
)


def test_strip_ansi_removes_color_codes():
    assert strip_ansi("\x1b[31mred\x1b[0m text") == "red text"


def test_dedup_adjacent_keeps_non_adjacent_dupes():
    text = "a\na\nb\na"
    assert dedup_adjacent(text) == "a\nb\na"


def test_trim_empty_edges_strips_newlines_only():
    assert trim_empty_edges("\n\nhello\nworld\n") == "hello\nworld"


def test_pretty_json_parses_and_indents():
    out = pretty_json('{"a":1,"b":[2,3]}')
    assert '"a": 1' in out and '"b": [\n' in out


def test_pretty_json_passthrough_on_invalid():
    bad = "not json {"
    assert pretty_json(bad) == bad


def test_head_lines_takes_first_n():
    assert head_lines("a\nb\nc\nd", n=2) == "a\nb"


def test_tail_lines_takes_last_n():
    assert tail_lines("a\nb\nc\nd", n=2) == "c\nd"


def test_head_tail_preserves_if_under_budget():
    text = "a\nb\nc"
    assert head_tail(text, head=2, tail=2) == text


def test_head_tail_elides_middle_with_marker():
    text = "\n".join(f"line{i}" for i in range(10))
    out = head_tail(text, head=2, tail=2)
    assert "line0" in out and "line1" in out
    assert "line8" in out and "line9" in out
    assert "lines elided" in out


def test_dedup_lines_preserves_order_drops_repeats():
    text = "x\ny\nx\nz\ny"
    assert dedup_lines(text) == "x\ny\nz"


def test_drop_regex_filters_matching_lines():
    text = "keep\ndrop me\nkeep\nplease drop"
    assert drop_regex(text, patterns=["^drop", "please"]) == "keep\nkeep"


def test_cap_chars_truncates_with_marker_above_threshold():
    text = "x" * 200
    out = cap_chars(text, n=50)
    assert out.startswith("x" * 50)
    assert "truncated" in out and "150 chars" in out


def test_cap_chars_passthrough_at_or_below_threshold():
    text = "x" * 50
    assert cap_chars(text, n=50) == text


def test_cap_lines_truncates_with_marker():
    text = "\n".join(f"l{i}" for i in range(10))
    out = cap_lines(text, n=3)
    assert out.startswith("l0\nl1\nl2")
    assert "7 lines elided" in out


def test_passthrough_returns_input():
    assert passthrough("anything") == "anything"


def test_apply_reducer_dispatches_by_kind():
    assert apply_reducer("head_lines", "a\nb\nc", {"n": 1}) == "a"


def test_apply_reducer_raises_on_unknown_kind():
    with pytest.raises(ValueError, match="unknown reducer kind"):
        apply_reducer("nope", "x", {})


def test_transforms_registry_lists_four_kinds():
    assert set(TRANSFORMS.keys()) == {
        "strip_ansi",
        "dedup_adjacent",
        "trim_empty_edges",
        "pretty_json",
    }


def test_reducers_registry_lists_eight_kinds():
    assert set(REDUCERS.keys()) == {
        "head_lines",
        "tail_lines",
        "head_tail",
        "dedup_lines",
        "drop_regex",
        "cap_chars",
        "cap_lines",
        "passthrough",
    }
