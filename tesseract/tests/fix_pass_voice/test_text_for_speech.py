"""Unit tests for `tesseract.voice.text_for_speech.to_spoken_text`."""

from __future__ import annotations

from tesseract.voice.text_for_speech import to_spoken_text


def test_plain_prose_unchanged():
    assert to_spoken_text("Hello there.") == "Hello there."


def test_empty_string_returns_empty():
    assert to_spoken_text("") == ""
    assert to_spoken_text("   \n  ") == ""


def test_drops_fenced_code_block():
    src = "Before block.\n```python\nprint('hi')\nx = 1\n```\nAfter block."
    out = to_spoken_text(src)
    assert "print" not in out
    assert "```" not in out
    assert "Before block." in out
    assert "After block." in out


def test_drops_unterminated_trailing_fence():
    # Streaming truncation case — fence opens but no close
    src = "Talking about code:\n```python\nprint('hi')"
    out = to_spoken_text(src)
    assert "print" not in out
    assert "```" not in out
    assert "Talking about code:" in out


def test_drops_lone_opening_fence_no_body():
    # Earliest streaming fragment — bare fence opens at sentence end
    # before any body lands. Must not leak literal backticks to TTS.
    assert to_spoken_text("Look at this: ```") == "Look at this:"
    assert to_spoken_text("Look at this: ```python") == "Look at this:"


def test_strips_inline_code_backticks_keeps_content():
    assert to_spoken_text("Use the `foo_bar` helper.") == "Use the foo_bar helper."


def test_strips_headers_keeps_text():
    assert to_spoken_text("# Title\nbody") == "Title body"
    assert to_spoken_text("### Sub") == "Sub"


def test_strips_bullet_markers_keeps_items():
    src = "- one\n- two\n- three"
    assert to_spoken_text(src) == "one two three"


def test_strips_asterisk_bullets_but_caller_still_sees_emphasis():
    # Asterisk bullets at line start get stripped; mid-line ** is left
    # to the provider's _sanitize_for_tts to handle.
    assert to_spoken_text("* alpha\n* beta") == "alpha beta"


def test_strips_numbered_list_markers():
    src = "1. first\n2. second\n10) tenth"
    assert to_spoken_text(src) == "first second tenth"


def test_drops_horizontal_rules():
    assert to_spoken_text("before\n---\nafter") == "before after"
    assert to_spoken_text("before\n***\nafter") == "before after"


def test_strips_html_tags_keeps_text():
    assert to_spoken_text("<span>hello</span>") == "hello"
    assert to_spoken_text("a<br/>b") == "ab"


def test_link_keeps_label_drops_url():
    assert to_spoken_text("see [docs](https://example.com) here") == "see docs here"


def test_image_dropped_entirely():
    assert to_spoken_text("hi ![alt text](pic.png) bye") == "hi bye"


def test_blockquote_marker_stripped():
    assert to_spoken_text("> quoted line") == "quoted line"


def test_only_code_block_returns_empty():
    src = "```\nfoo\n```"
    assert to_spoken_text(src) == ""


def test_collapses_whitespace():
    assert to_spoken_text("a   b\n\n\nc") == "a b c"


def test_idempotent_on_clean_prose():
    s = "This is fine prose, with commas, and a period."
    assert to_spoken_text(to_spoken_text(s)) == to_spoken_text(s)


def test_mixed_markdown_realistic():
    src = (
        "## Step 1\n"
        "Use `pytest` to run tests:\n"
        "```bash\n"
        "pytest -xvs\n"
        "```\n"
        "Then check the output."
    )
    out = to_spoken_text(src)
    assert "Step 1" in out
    assert "pytest" in out  # stripped from inline code
    assert "Use" in out
    assert "Then check the output." in out
    assert "```" not in out
    assert "##" not in out
    assert "-xvs" not in out
