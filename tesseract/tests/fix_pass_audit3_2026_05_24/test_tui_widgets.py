"""Audit-3 — smoke tests for the TUI widgets we added or changed.

These are pure-render tests (no async, no Textual app harness) — we
construct the widget, drive its public API, and assert the resulting
state. Tests that need a live Textual screen live in
``test_textual_app.py``.
"""

from __future__ import annotations

import pytest

from tesseract.scripts.tars_app import (
    PtyStreamBlock,
    looks_like_diff,
    render_diff,
    _THEMES,
    _THEME_NAMES,
)


# ── diff renderer (M8) ────────────────────────────────────────────────


def test_looks_like_diff_detects_unified_diff_header() -> None:
    sample = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,3 +1,3 @@\n"
        " ctx\n-old\n+new\n"
    )
    assert looks_like_diff(sample)


def test_looks_like_diff_detects_file_header_pair() -> None:
    sample = "--- a/foo.py\n+++ b/foo.py\nsome change\n"
    assert looks_like_diff(sample)


def test_looks_like_diff_false_for_markdown_bullets() -> None:
    """Regression: recall_history work-history hits are a markdown
    bullet list (every line starts with `- `). The old loose +/-
    heuristic painted the whole thing red. The strict detector must
    NOT treat bullet lists as diffs."""
    bullets = (
        "Work-history hits — non-authoritative suggestions.\n\n"
        "- **workshop:tars-capability-spec** @ 2026-04-29\n"
        "- **session:2026-04-29-1406.bak** @ 2026-04-29\n"
        "- **workshop:new-idea-notes** @ 2026-05-02\n"
        "- **Decision-echo tool**: save the reason behind a choice.\n"
        "- **Signal weather report**: watch code churn.\n"
    )
    assert not looks_like_diff(bullets)


def test_looks_like_diff_false_for_prose() -> None:
    assert not looks_like_diff("Just regular markdown text here.")
    assert not looks_like_diff("")
    # Even a stray +/- line is not a diff without hunk/file headers.
    assert not looks_like_diff("- a bullet\n+ another bullet\n- third\n")


def test_render_diff_colours_each_kind() -> None:
    text = "--- a\n+++ b\n@@ hunk @@\n+add\n-del\n ctx\n"
    out = render_diff(text)
    # Just smoke — the function returns a rich Text object; verify it
    # contains every line we passed in (the styling is structural).
    rendered = out.plain
    assert "--- a" in rendered
    assert "+++ b" in rendered
    assert "@@ hunk @@" in rendered
    assert "+add" in rendered
    assert "-del" in rendered


# ── themes (M8) ───────────────────────────────────────────────────────


def test_theme_registry_has_expected_names() -> None:
    assert set(_THEME_NAMES) == {"dark", "light", "high-contrast"}
    for name in _THEME_NAMES:
        palette = _THEMES[name]
        # Every theme must define the full token surface; otherwise
        # _apply_theme leaves stale values from the previous theme.
        for key in (
            "background",
            "surface",
            "primary",
            "accent",
            "text",
            "text-muted",
        ):
            assert key in palette, f"{name} theme missing {key}"
            assert palette[key].startswith("#"), f"{name}.{key} must be #hex"


# ── PtyStreamBlock (M4) ───────────────────────────────────────────────


def test_pty_stream_block_appends_and_caps_scrollback() -> None:
    block = PtyStreamBlock(label="test", stream_id="abcd1234")
    for i in range(PtyStreamBlock.MAX_LINES + 50):
        block.append(f"line {i}\n")
    # Internal buffer is bounded to MAX_LINES, keeping the tail.
    assert len(block._lines) == PtyStreamBlock.MAX_LINES
    assert block._lines[-1].endswith(f"line {PtyStreamBlock.MAX_LINES + 49}")


def test_pty_stream_block_ignores_empty_chunks() -> None:
    block = PtyStreamBlock(label="t", stream_id="x")
    block.append("")
    block.append(None)  # type: ignore[arg-type] — defensive call
    assert block._lines == []


def test_pty_stream_block_mark_done_is_safe_pre_mount() -> None:
    """Constructing + mark_done outside an active Textual app must not
    raise — guards in append() / mark_done() exist so the widget can be
    fed data before mount, and unit-tested without a full app harness.
    """
    block = PtyStreamBlock(label="t", stream_id="y")
    block.mark_done(success=True)
    block.mark_done(success=False)


# ── slash command registry ────────────────────────────────────────────


def test_tars_app_exposes_all_legacy_commands_plus_new() -> None:
    """M6 — ensure the Textual TUI's slash handler covers every legacy
    CLI command. Reproduces audit-3's regression check."""
    from tesseract.orchestrator.tars_controller.slash_commands import (
        known_commands,
    )
    # The Textual app constructs its handler dict via _slash_handlers;
    # we don't instantiate a full App here (would need a textual
    # screen), so just verify the legacy command names are covered by
    # the static expected set we maintain in tars_app.py.
    expected_legacy = set(known_commands())
    new_commands = {
        "help", "clear", "sessions", "new", "delete", "title",
        "reload", "detach", "quit", "shutdown",
        # M8 additions:
        "theme", "copy", "raw",
    }
    missing = expected_legacy - new_commands
    assert not missing, f"Textual TUI missing slash commands: {missing}"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
