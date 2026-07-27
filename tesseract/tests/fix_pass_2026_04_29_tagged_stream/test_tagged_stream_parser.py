"""Structured-tag stream parser tests — `_parse_tagged_stream` /
`_split_text_for_surfaces` in `tesseract/mirror/server/stream_parser.py`.

The model is instructed (via `prompt._OUTPUT_CONTRACT_RULE_TEXT`) to wrap
every text emission in `<intent>...</intent>` or `<answer>...</answer>`.
The parser is a streaming state machine over that contract — replaces the
old regex-based heuristic which mis-classified "now I'll explain..." as
intent and "Searching the docs..." mid-paragraph as answer.

Coverage: complete tags, multi-delta partial-tag carry, intent→tools→intent
sequencing, untagged-text degradation, mismatched close, multi-tag in one
buffer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tesseract.mirror.server.stream_parser import (
    _parse_tagged_stream,
    _partial_tag_suffix_len,
    _split_text_for_surfaces,
)


@dataclass
class _StubSession:
    stream_status_buffer: str = ""
    stream_tag_state: str = "outside"
    stream_untagged_warned: bool = False


def test_single_intent_tag_in_one_delta():
    pieces, state, carry = _parse_tagged_stream("<intent>Checking memory.</intent>", "outside")
    assert pieces == [("intent", "Checking memory.")]
    assert state == "outside"
    assert carry == ""


def test_single_answer_tag_in_one_delta():
    pieces, state, carry = _parse_tagged_stream("<answer>Found three entries.</answer>", "outside")
    assert pieces == [("answer", "Found three entries.")]
    assert state == "outside"
    assert carry == ""


def test_intent_then_answer_one_buffer():
    text = "<intent>Searching.</intent><answer>Got it — here is the summary.</answer>"
    pieces, state, carry = _parse_tagged_stream(text, "outside")
    assert pieces == [
        ("intent", "Searching."),
        ("answer", "Got it — here is the summary."),
    ]
    assert state == "outside"
    assert carry == ""


def test_partial_tag_at_end_holds_in_carry():
    # `<int` could be the start of `<intent>` — must be held, not emitted.
    pieces, state, carry = _parse_tagged_stream("hello <int", "answer")
    assert pieces == [("answer", "hello ")]
    assert state == "answer"
    assert carry == "<int"


def test_carry_completes_on_next_delta():
    sess = _StubSession()
    out_a = _split_text_for_surfaces(sess, sess,"<inten")
    assert out_a == []
    assert sess.stream_status_buffer == "<inten"
    out_b = _split_text_for_surfaces(sess, sess,"t>Reading.</intent>")
    assert out_b == [("intent", "Reading.")]
    assert sess.stream_status_buffer == ""
    assert sess.stream_tag_state == "outside"


def test_lone_lt_is_held_as_partial():
    # A bare "<" at end of delta could be the start of any tag.
    pieces, state, carry = _parse_tagged_stream("text<", "answer")
    assert pieces == [("answer", "text")]
    assert state == "answer"
    assert carry == "<"


def test_lt_followed_by_non_tag_is_emitted():
    # "<x" cannot be any of our tags — emit as untagged in outside state.
    pieces, state, carry = _parse_tagged_stream("<x", "outside")
    assert pieces == [("_untagged", "<x")]
    assert state == "outside"
    assert carry == ""


def test_streaming_intent_across_many_deltas():
    sess = _StubSession()
    deltas = ["<int", "ent>Chec", "king the v", "ault.</int", "ent>"]
    pieces_collected: list[tuple[str, str]] = []
    for d in deltas:
        pieces_collected.extend(_split_text_for_surfaces(sess, sess,d))
    text = "".join(t for k, t in pieces_collected if k == "intent")
    assert text == "Checking the vault."
    assert sess.stream_tag_state == "outside"
    assert sess.stream_status_buffer == ""


def test_intent_then_tool_then_intent_then_answer():
    """The contract allows intent → tool → intent → tool → answer. The
    parser is stateless about tools — they fire on the function-call
    channel — but multiple intent blocks must classify cleanly."""
    text = (
        "<intent>Reading the file.</intent>"
        "<intent>Now searching the wiki.</intent>"
        "<answer>Both done — summary follows.</answer>"
    )
    pieces, state, carry = _parse_tagged_stream(text, "outside")
    assert [k for k, _ in pieces] == ["intent", "intent", "answer"]
    assert state == "outside"
    assert carry == ""


def test_untagged_text_degrades_to_answer_with_warning():
    sess = _StubSession()
    # Model violates the contract — emits prose with no wrapper.
    out = _split_text_for_surfaces(sess, sess,"Just answering directly without tags.")
    assert out == [("answer", "Just answering directly without tags.")]
    assert sess.stream_untagged_warned is True


def test_untagged_warning_fires_only_once_per_turn():
    sess = _StubSession()
    _split_text_for_surfaces(sess, sess,"naked text 1.")
    _split_text_for_surfaces(sess, sess,"naked text 2.")
    # Both deltas are untagged but only the first should set the flag —
    # second call sees flag already True and does not re-warn.
    assert sess.stream_untagged_warned is True


def test_state_persists_across_deltas_inside_tag():
    sess = _StubSession()
    out_a = _split_text_for_surfaces(sess, sess,"<answer>Part one.")
    out_b = _split_text_for_surfaces(sess, sess," Part two.</answer>")
    assert out_a == [("answer", "Part one.")]
    assert out_b == [("answer", " Part two.")]
    assert sess.stream_tag_state == "outside"


def test_intent_carry_partial_close_tag():
    sess = _StubSession()
    out_a = _split_text_for_surfaces(sess, sess,"<intent>hi</int")
    out_b = _split_text_for_surfaces(sess, sess,"ent>")
    assert out_a == [("intent", "hi")]
    assert out_b == []
    assert sess.stream_tag_state == "outside"


def test_partial_tag_suffix_len_recognises_each_token_prefix():
    assert _partial_tag_suffix_len("foo<") == 1
    assert _partial_tag_suffix_len("foo<i") == 2
    assert _partial_tag_suffix_len("foo<inten") == 6
    assert _partial_tag_suffix_len("foo</answer") == 8
    assert _partial_tag_suffix_len("foo<x") == 0
    assert _partial_tag_suffix_len("plain text") == 0
    assert _partial_tag_suffix_len("") == 0


def test_consecutive_same_kind_pieces_are_coalesced_in_split():
    """Internal pieces from `_parse_tagged_stream` of the same surface
    should merge into one envelope — keeps the frontend rAF-batched
    stream from doing extra work for free."""
    sess = _StubSession()
    out = _split_text_for_surfaces(sess, sess,"<answer>one</answer><answer>two</answer>")
    assert out == [("answer", "onetwo")]


def test_empty_delta_returns_no_pieces():
    sess = _StubSession()
    assert _split_text_for_surfaces(sess, sess,"") == []
    assert sess.stream_tag_state == "outside"


def test_normalises_escaped_newlines():
    sess = _StubSession()
    out = _split_text_for_surfaces(sess, sess,"<answer>line one\\nline two</answer>")
    assert out == [("answer", "line one\nline two")]


def test_mismatched_close_intent_with_answer_closes_gracefully():
    """Contract-violation case: model emits </answer> while still inside
    <intent>. The state machine treats any close token as a transition
    back to "outside" — the prior intent content is already emitted, the
    surface is closed, and the next opening tag is honored normally. The
    parser must not crash, lose content, or leave a dangling state."""
    pieces, state, carry = _parse_tagged_stream("<intent>hi</answer>", "outside")
    assert pieces == [("intent", "hi")]
    assert state == "outside"
    assert carry == ""


def test_mismatched_close_answer_with_intent_closes_gracefully():
    pieces, state, carry = _parse_tagged_stream("<answer>final reply</intent>", "outside")
    assert pieces == [("answer", "final reply")]
    assert state == "outside"
    assert carry == ""


def test_stray_close_tag_in_outside_state_is_dropped():
    """A close tag with no matching open is harmless — state was already
    "outside", remains "outside". Any leading prose surfaces as untagged."""
    pieces, state, carry = _parse_tagged_stream("stray</intent>then more", "outside")
    assert pieces == [("_untagged", "stray"), ("_untagged", "then more")]
    assert state == "outside"
    assert carry == ""
