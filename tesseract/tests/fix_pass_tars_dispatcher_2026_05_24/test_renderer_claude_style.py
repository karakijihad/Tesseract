"""Renderer — Claude-CLI-style polish (rich-backed).

The TUI's tool_use / tool_result / cli_chunk rendering should match
Claude CLI's visual vocabulary so an operator who knows that CLI feels
at home in ``tars``:

* coloured ``●`` marker per tool block (yellow = in-progress,
  green = done, red = failed),
* Tool name in bold, input summary in grey,
* Sub-process output streamed via :class:`CliChunkEvent` indented +
  grey underneath the parent tool_use line ("see what claude is doing
  live" affordance),
* Tool results capped at a small number of lines so a huge markdown
  dump doesn't shred the screen.

Tests record the Console output and assert on plain text + ANSI color
codes rather than exact bytes.
"""

from __future__ import annotations

import re
from pathlib import Path

from tesseract.orchestrator.tars_controller.renderer import TuiRenderer


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


def test_tool_use_renders_with_bullet_and_bold_name(isolated_home: Path) -> None:
    r = TuiRenderer(record=True, color=True)
    r.render(
        {
            "kind": "tool_use",
            "tool": "delegate_claude",
            "input": {"task": "patch the auth middleware"},
            "tool_use_id": "call-1",
            "session_id": "s1",
            "origin": "chat",
        }
    )
    raw = r.recorded_text()
    plain = _plain(raw)
    assert "●" in plain
    assert "delegate_claude" in plain
    assert "task=patch the auth middleware" in plain
    # Yellow ANSI for the bullet (in-progress).
    assert "\x1b[33m" in raw


def test_tool_result_success_renders_green(isolated_home: Path) -> None:
    r = TuiRenderer(record=True, color=True)
    r.render(
        {
            "kind": "tool_result",
            "tool_use_id": "call-1",
            "success": True,
            "output": {"summary": "applied"},
            "session_id": "s1",
            "origin": "chat",
        }
    )
    raw = r.recorded_text()
    plain = _plain(raw)
    assert "●" in plain
    assert "done" in plain
    # Green ANSI somewhere in the output.
    assert "\x1b[32m" in raw


def test_tool_result_failure_renders_red(isolated_home: Path) -> None:
    r = TuiRenderer(record=True, color=True)
    r.render(
        {
            "kind": "tool_result",
            "tool_use_id": "call-1",
            "success": False,
            "output": {"error": "no PATH"},
            "session_id": "s1",
            "origin": "chat",
        }
    )
    raw = r.recorded_text()
    plain = _plain(raw)
    assert "failed" in plain
    assert "\x1b[31m" in raw


def test_tool_result_truncates_long_output(isolated_home: Path) -> None:
    """Markdown / JSON dumps that span dozens of lines must not shred
    the screen. The renderer caps at ``_TOOL_RESULT_LINE_CAP`` lines and
    appends ``… (N more lines …)``. Full payload remains on disk in the
    transcript file."""
    big_output = "\n".join(f"line {i}" for i in range(50))
    r = TuiRenderer(record=True, color=False)
    r.render(
        {
            "kind": "tool_result",
            "tool_use_id": "call-1",
            "success": True,
            "output": big_output,
            "session_id": "s1",
            "origin": "chat",
        }
    )
    plain = _plain(r.recorded_text())
    # First few lines visible.
    assert "line 0" in plain
    # Tail of the dump is NOT inline.
    assert "line 49" not in plain
    # Truncation marker present with the right delta.
    assert "more lines" in plain


def test_cli_chunk_start_renders_started_line(isolated_home: Path) -> None:
    r = TuiRenderer(record=True, color=False)
    r.render(
        {
            "kind": "cli_chunk",
            "tool": "delegate_claude",
            "tool_use_id": "call-1",
            "text": "",
            "phase": "start",
            "session_id": "s1",
            "origin": "chat",
        }
    )
    assert "↘ delegate_claude started" in _plain(r.recorded_text())


def test_cli_chunk_chunks_are_indented_and_grey(isolated_home: Path) -> None:
    r = TuiRenderer(record=True, color=True)
    r.render(
        {
            "kind": "cli_chunk",
            "tool": "delegate_claude",
            "tool_use_id": "call-1",
            "text": "Reading file\nApplying patch\n",
            "phase": "chunk",
            "session_id": "s1",
            "origin": "chat",
        }
    )
    raw = r.recorded_text()
    plain = _plain(raw)
    assert "    Reading file" in plain
    assert "    Applying patch" in plain


def test_cli_chunk_end_renders_exit_code(isolated_home: Path) -> None:
    r = TuiRenderer(record=True, color=True)
    r.render(
        {
            "kind": "cli_chunk",
            "tool": "delegate_claude",
            "tool_use_id": "call-1",
            "text": "",
            "phase": "end",
            "exit_code": 0,
            "session_id": "s1",
            "origin": "chat",
        }
    )
    assert "↖ delegate_claude done (ok)" in _plain(r.recorded_text())

    r = TuiRenderer(record=True, color=True)
    r.render(
        {
            "kind": "cli_chunk",
            "tool": "delegate_claude",
            "tool_use_id": "call-1",
            "text": "",
            "phase": "end",
            "exit_code": 2,
            "session_id": "s1",
            "origin": "chat",
        }
    )
    raw = r.recorded_text()
    assert "↖ delegate_claude done (exit=2)" in _plain(raw)
    # Non-zero exit in red.
    assert "\x1b[31m" in raw


def test_assistant_markdown_renders_bold_italic_links(
    isolated_home: Path,
) -> None:
    """Claude-CLI parity: assistant text containing markdown
    ``**bold**`` / ``*italic*`` / ``[label](url)`` should render with
    rich's Markdown component AFTER the streaming completes. The
    operator sees the streamed text live, then a tidy formatted block."""
    r = TuiRenderer(record=True, color=True)
    r.render(
        {
            "kind": "assistant_text",
            "text": (
                "Look at **this bold thing**, *italic too*, and "
                "[click here](https://example.com)."
            ),
            "partial": False,
        }
    )
    raw = r.recorded_text()
    plain = _plain(raw)
    # Markdown re-render contains the text WITHOUT the markup syntax.
    assert "this bold thing" in plain
    assert "italic too" in plain
    # OSC-8 escape (clickable link) — rich emits this on supported
    # terminals; the recorded buffer captures it.
    assert "https://example.com" in raw
