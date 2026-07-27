"""Renderer — `<answer>` / `<intent>` persona-tag handling.

TARS replies are wrapped in persona tags. The renderer:

* strips ``<answer>`` / ``</answer>`` (text inside renders as the normal
  magenta reply),
* dims content inside ``<intent>`` ... ``</intent>`` blocks so operators
  visually distinguish "TARS thinking out loud" from the actual answer,
* passes any other tag through unchanged so future persona-tag additions
  don't disappear silently.

The streaming path holds back partial tags across chunks so a tag split
mid-byte still parses correctly.

These tests record the Console output via ``record=True`` and assert on
the visible text after stripping ANSI — the renderer's job is to PRODUCE
a clean transcript, the exact escape sequences are an implementation
detail of ``rich``.
"""

from __future__ import annotations

import re
from pathlib import Path

from tesseract.orchestrator.tars_controller.renderer import (
    TuiRenderer,
    _AssistantStreamParser,
)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\][^\x1b]*\x1b\\")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ── parser unit tests (segment-tuple contract) ────────────────────────


def test_parser_strips_answer_tags(isolated_home: Path) -> None:
    p = _AssistantStreamParser()
    segs = list(p.feed("<answer>hello world</answer>")) + list(p.flush())
    # Tags don't appear in any segment; content does.
    assert "".join(text for _, text in segs) == "hello world"


def test_parser_marks_intent_block_with_grey_style(isolated_home: Path) -> None:
    p = _AssistantStreamParser()
    segs = list(p.feed("<intent>thinking</intent>after")) + list(p.flush())
    # Intent segment gets the dim style; the after-segment doesn't.
    intent_seg = next(s for s in segs if s[1] == "thinking")
    after_seg = next(s for s in segs if s[1] == "after")
    assert intent_seg[0] == "grey50"
    assert after_seg[0] is None


def test_parser_passes_unknown_tags_through(isolated_home: Path) -> None:
    p = _AssistantStreamParser()
    segs = list(p.feed("hi <bogus>x</bogus> bye")) + list(p.flush())
    joined = "".join(text for _, text in segs)
    assert "<bogus>" in joined and "</bogus>" in joined


def test_parser_handles_tag_split_across_chunks(isolated_home: Path) -> None:
    p = _AssistantStreamParser()
    segs = []
    segs.extend(p.feed("before <ans"))
    segs.extend(p.feed("wer>inside</answer> after"))
    segs.extend(p.flush())
    assert "".join(text for _, text in segs) == "before inside after"


def test_parser_intent_split_across_chunks(isolated_home: Path) -> None:
    p = _AssistantStreamParser()
    segs = []
    for chunk in ("a<inte", "nt>b</int", "ent>c"):
        segs.extend(p.feed(chunk))
    segs.extend(p.flush())
    joined = "".join(text for _, text in segs)
    assert joined == "abc"
    # `b` is styled grey; `a` and `c` are plain.
    b_seg = next(s for s in segs if s[1] == "b")
    assert b_seg[0] == "grey50"


def test_parser_stray_open_bracket_does_not_block(isolated_home: Path) -> None:
    p = _AssistantStreamParser()
    segs = list(p.feed("price < 10 is fine")) + list(p.flush())
    joined = "".join(text for _, text in segs)
    assert "price < 10 is fine" in joined


# ── end-to-end renderer tests ─────────────────────────────────────────


def test_renderer_streamed_answer_strips_tags_end_to_end(
    isolated_home: Path,
) -> None:
    r = TuiRenderer(record=True, color=True)
    r.render(
        {"kind": "assistant_text", "text": "<answer>hi ", "partial": True}
    )
    r.render(
        {"kind": "assistant_text", "text": "there</answer>", "partial": False}
    )
    plain = _plain(r.recorded_text())
    assert "<answer>" not in plain
    assert "</answer>" not in plain
    assert "hi there" in plain


def test_renderer_streamed_intent_then_answer(isolated_home: Path) -> None:
    r = TuiRenderer(record=True, color=True)
    r.render(
        {
            "kind": "assistant_text",
            "text": "<intent>plan: do X</intent><answer>doing X</answer>",
            "partial": False,
        }
    )
    plain = _plain(r.recorded_text())
    assert "<intent>" not in plain
    assert "<answer>" not in plain
    assert "plan: do X" in plain
    assert "doing X" in plain


def test_renderer_parser_resets_between_turns(isolated_home: Path) -> None:
    """An unclosed intent in turn N must not leak into turn N+1."""
    r = TuiRenderer(record=True, color=True)
    r.render(
        {
            "kind": "assistant_text",
            "text": "<intent>oops never closed",
            "partial": False,
        }
    )
    r.render(
        {
            "kind": "assistant_text",
            "text": "<answer>fresh turn</answer>",
            "partial": False,
        }
    )
    plain = _plain(r.recorded_text())
    # Both turns' content is present; neither tag survives.
    assert "oops never closed" in plain
    assert "fresh turn" in plain
    assert "<intent>" not in plain
    assert "<answer>" not in plain
