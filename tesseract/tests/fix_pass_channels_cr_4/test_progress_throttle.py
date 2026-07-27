"""CR-4 — :class:`ProgressThrottler` enforces 1 Hz / chat with coalescing."""

from __future__ import annotations

import asyncio

import pytest

from tesseract.integrations._channel_progress import (
    ProgressEvent,
    ProgressThrottler,
    emoji_for_tool,
    format_progress_line,
)


@pytest.mark.asyncio
async def test_rapid_emits_collapse_to_single_edit_within_cooldown():
    """Three emits within 200ms must produce 1 immediate edit + 1 buffered
    edit after the cooldown elapses — never three. The buffered edit
    must carry the LAST text (no stale-text leakage)."""
    edits: list[str] = []

    async def _edit(text: str) -> None:
        edits.append(text)

    throttler = ProgressThrottler(_edit, cooldown_s=1.0)
    await throttler.emit("first")
    await throttler.emit("second")
    await throttler.emit("third")

    # First emit fires immediately; the rest fold into the buffer.
    assert edits == ["first"]

    # Let the flush task fire (cooldown=1.0s). 1.1s is plenty.
    await asyncio.sleep(1.1)
    assert edits == ["first", "third"]
    await throttler.stop()


@pytest.mark.asyncio
async def test_stop_flushes_pending_text_once():
    """A throttler.stop() before the cooldown elapses must flush the
    buffered emit so the operator still sees the latest tool_start /
    tool_end edit before the final-reply path overwrites the placeholder.

    Pre-2026-05-19 the throttler dropped the buffer on stop() — short
    text-only turns reached the final reply without ever surfacing a
    progress line, making Telegram feel stuck. Now stop() flushes
    exactly once and any pending-flush task is cancelled so the buffer
    is never double-delivered.
    """
    edits: list[str] = []

    async def _edit(text: str) -> None:
        edits.append(text)

    throttler = ProgressThrottler(_edit, cooldown_s=1.0)
    await throttler.emit("first")
    await throttler.emit("buffered_should_flush_on_stop")
    assert edits == ["first"]

    await throttler.stop()
    assert edits == ["first", "buffered_should_flush_on_stop"]

    # The cancelled flush task must not fire afterwards — exactly one
    # post-stop edit, no duplicates.
    await asyncio.sleep(1.2)
    assert edits == ["first", "buffered_should_flush_on_stop"]


@pytest.mark.asyncio
async def test_stop_without_pending_is_noop_on_edit_path():
    """stop() with no buffered text must not fire any extra edit."""
    edits: list[str] = []

    async def _edit(text: str) -> None:
        edits.append(text)

    throttler = ProgressThrottler(_edit, cooldown_s=1.0)
    await throttler.emit("only")
    assert edits == ["only"]

    await throttler.stop()
    assert edits == ["only"]


@pytest.mark.asyncio
async def test_invoke_edit_skipped_when_closed():
    """If ``stop()`` runs after a flush captures its text but before
    ``_invoke_edit`` fires, the edit must be skipped — no ghost line
    lands after the operator-visible final reply."""
    fired: list[str] = []

    async def _slow_edit(text: str) -> None:
        await asyncio.sleep(0.05)
        fired.append(text)

    throttler = ProgressThrottler(_slow_edit, cooldown_s=1.0)
    await throttler.emit("first")
    fired.clear()  # drop the immediate edit; we care about post-stop
    # Closing before any subsequent emit must make the gate trip even
    # if a caller calls _invoke_edit directly (defense in depth).
    await throttler.stop()
    await throttler._invoke_edit("ghost")
    assert fired == []


@pytest.mark.asyncio
async def test_blank_emits_ignored():
    edits: list[str] = []

    async def _edit(text: str) -> None:
        edits.append(text)

    throttler = ProgressThrottler(_edit, cooldown_s=0.5)
    await throttler.emit("")
    await throttler.emit("   ")
    assert edits == []
    await throttler.stop()


def test_format_elapsed_renders_human_seconds():
    line = format_progress_line(ProgressEvent(kind="elapsed", elapsed_s=30.0))
    assert "30s" in line
    assert "still working" in line


def test_format_tool_start_web_search_includes_query():
    line = format_progress_line(ProgressEvent(
        kind="tool_start",
        tool_name="web_search",
        tool_args={"query": "El Niño 2026"},
    ))
    assert "🔍" in line
    assert "El Niño 2026" in line


def test_format_tool_start_delegate_uses_handshake_emoji():
    line = format_progress_line(ProgressEvent(
        kind="tool_start",
        tool_name="delegate_claude",
    ))
    assert "🤝" in line
    assert "delegate_claude" in line


def test_emoji_map_known_and_unknown():
    assert emoji_for_tool("vault_query") == "📂"
    assert emoji_for_tool("memory_search") == "🧠"
    assert emoji_for_tool("brief_render") == "📰"
    assert emoji_for_tool("delegate_codex") == "🤝"
    assert emoji_for_tool("delegate_anything") == "🤝"
    # Unknown name falls through to the generic default emoji.
    assert emoji_for_tool("never_heard_of_this_tool") == "🛠"
    assert emoji_for_tool("") == "🛠"
