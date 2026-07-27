"""2026-05-17 — source-side cap on `recall_for_inbound` output.

Defense-in-depth alongside `_trim_to_budget`. `_cap_recall_block` keeps
the recall block under `_RECALL_TOTAL_MAX_CHARS` so even pathological
inputs (legacy uncapped summary bullets, 5 oversized memory bodies)
don't push the prompt past the codex char cap.
"""

from __future__ import annotations

from tesseract.integrations._chat_memory import (
    _RECALL_TOTAL_MAX_CHARS,
    _cap_recall_block,
)


def test_passes_through_when_under_cap() -> None:
    block = "small recall content"
    assert _cap_recall_block(block) == block


def test_truncates_with_marker_when_over_cap() -> None:
    block = "x" * (_RECALL_TOTAL_MAX_CHARS * 2)
    out = _cap_recall_block(block)
    assert len(out) <= _RECALL_TOTAL_MAX_CHARS
    assert "capped" in out


def test_empty_string_passthrough() -> None:
    assert _cap_recall_block("") == ""
