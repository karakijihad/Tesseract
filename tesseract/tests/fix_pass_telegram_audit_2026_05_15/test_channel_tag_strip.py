"""Audit follow-up — channel sessions never leak ``<intent>`` / ``<answer>`` tags.

Three regressions guarded:

1. ``_extract_channel_reply`` returns the answer when both are present.
2. ``_extract_channel_reply`` returns the intent text (without tags) when
   the stream is truncated after ``<intent>`` and never opens ``<answer>``
   — the pre-fix code returned the raw text including literal tags, which
   showed up on the phone as ``<intent>Checking…</intent>``.
3. ``_split_text_for_surfaces`` short-circuits for channel sessions so a
   future refactor that re-routes channel chunks through ``_run_turn``
   cannot strand text in the tag-state-machine carry buffer.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tesseract.mirror.server.stream_parser import (
    _extract_channel_reply,
    _split_text_for_surfaces,
    _strip_known_tags,
)


def test_extract_returns_answer_when_present() -> None:
    raw = "<intent>thinking</intent><answer>Hello, world.</answer>"
    assert _extract_channel_reply(raw) == "Hello, world."


def test_extract_falls_back_to_untagged_text() -> None:
    raw = "Just answering directly."
    assert _extract_channel_reply(raw) == "Just answering directly."


def test_extract_returns_intent_when_only_intent_emitted() -> None:
    """Pre-fix bug: stream truncated after ``<intent>`` left literal tags
    visible on the phone. Now we strip them and surface the intent text
    so the user sees something readable."""
    raw = "<intent>Checking the vault for that.</intent>"
    out = _extract_channel_reply(raw)
    assert out == "Checking the vault for that."
    assert "<intent>" not in out
    assert "</intent>" not in out


def test_extract_strips_orphan_tags_as_last_resort() -> None:
    """Malformed close: no parser piece matches → last-resort tag strip."""
    raw = "<intent>broken"  # never closes
    out = _extract_channel_reply(raw)
    # The parser will treat ``<intent>broken`` as carry — pieces is empty,
    # so the last-resort path drops the bare ``<intent>`` opener.
    assert "<intent>" not in out
    # Reviewer P0-1: the user-visible text must survive. The fallback
    # path strips tags from the raw input rather than from the parser
    # carry — confirm the text content is preserved.
    assert "broken" in out


def test_extract_empty_input_yields_empty() -> None:
    """A turn that produced zero text must not return literal markup."""
    assert _extract_channel_reply("") == ""
    assert _extract_channel_reply("<intent>").strip() == ""


def test_strip_known_tags_handles_mixed_case() -> None:
    assert _strip_known_tags("<Intent>x</Intent>") == "x"
    assert _strip_known_tags("a <answer>b</answer> c") == "a b c"


def test_split_text_for_surfaces_skips_tag_machine_for_channel_sessions() -> None:
    sess = MagicMock()
    sess.chat_session = MagicMock()
    sess.chat_session.session_kind = "channel"
    sess.stream_status_buffer = ""
    sess.stream_tag_state = "outside"

    out = _split_text_for_surfaces(sess, sess, "<intent>x</intent>")
    # Channel session — text is forwarded verbatim as a single ``answer``
    # piece. The tag-state-machine NEVER runs (no carry, no warning).
    assert out == [("answer", "<intent>x</intent>")]
    assert sess.stream_status_buffer == ""
    assert sess.stream_tag_state == "outside"


def test_split_text_for_surfaces_still_parses_for_cockpit_sessions() -> None:
    sess = MagicMock()
    sess.chat_session = MagicMock()
    sess.chat_session.session_kind = "cockpit"
    sess.stream_status_buffer = ""
    sess.stream_tag_state = "outside"
    sess.stream_untagged_warned = False

    out = _split_text_for_surfaces(sess, sess, "<intent>hi</intent><answer>ok</answer>")
    surfaces = [kind for kind, _ in out]
    assert "intent" in surfaces
    assert "answer" in surfaces
