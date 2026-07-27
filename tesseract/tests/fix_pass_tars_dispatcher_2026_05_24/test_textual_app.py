"""Textual TUI smoke + widget tests.

Drives the app via Textual's :class:`~textual.app.App.run_test` pilot
context. We construct ``TarsApp`` with a stub IPC client that produces
no live pushes (its inbox queue stays empty), then call the same
``_render_event`` hooks the push loop would call. That lets us assert
on the widget tree without spinning a real daemon.

Things covered:

* Layout — TranscriptView + StatusBar + Input mount cleanly.
* Each event kind produces the right widget class.
* ``ToolBlock`` collapses by default, expands on failure.
* ``AssistantMessage`` strips persona tags before rendering.
* ``StatusBar`` reflects active-tool state.
* Slash commands (``:approve``, ``:deny``, ``:quit``) dispatch via the
  client without going through real input handling.

Pure-helper tests (``strip_persona_tags``, ``summarize_input``) are
straight-line — no app needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from types import SimpleNamespace

from tesseract.scripts.tars_app import (
    AssistantMessage,
    StatusBar,
    TarsApp,
    ThinkingIndicator,
    ToolBlock,
    TranscriptView,
    UserMessage,
    render_persona_segments,
    strip_persona_tags,
    summarize_input,
)


# ── pure helpers ──────────────────────────────────────────────────────


def test_strip_persona_tags_removes_answer(isolated_home: Path) -> None:
    assert strip_persona_tags("<answer>hi</answer>") == "hi"


def test_strip_persona_tags_unwraps_intent_as_blockquote(
    isolated_home: Path,
) -> None:
    out = strip_persona_tags("<intent>thinking</intent>after")
    # Intent block becomes a Markdown blockquote so the rich Markdown
    # renderer still distinguishes it from the answer.
    assert "> thinking" in out


def test_intent_renders_grey_italic_not_red(isolated_home: Path) -> None:
    """Codex-style intent (<intent>…</intent>) must NOT render as a hot-red
    Markdown blockquote — the operator wants calm light-grey italic."""
    from rich.text import Text as RichText

    out = render_persona_segments(
        "<intent>Running a quick safety check on the vault.</intent>\n\nFound issues."
    )
    renderables = list(getattr(out, "renderables", [out]))
    intent = next((r for r in renderables if isinstance(r, RichText)), None)
    assert intent is not None, "intent segment should render as styled Text"
    assert intent.plain == "Running a quick safety check on the vault."
    style = str(intent.style).lower()
    assert "italic" in style
    assert "red" not in style and "magenta" not in style


def test_intent_body_renders_as_markdown(isolated_home: Path) -> None:
    from rich.markdown import Markdown as RichMarkdown

    out = render_persona_segments("<intent>x</intent>\n\n**bold** body")
    renderables = list(getattr(out, "renderables", [out]))
    assert any(isinstance(r, RichMarkdown) for r in renderables)


def test_render_persona_segments_plain_answer_no_intent(isolated_home: Path) -> None:
    from rich.markdown import Markdown as RichMarkdown

    out = render_persona_segments("<answer>just a reply</answer>")
    # No intent → a single Markdown renderable (answer wrapper stripped).
    assert isinstance(out, RichMarkdown)


def test_summarize_input_truncates_long_strings(isolated_home: Path) -> None:
    out = summarize_input({"task": "x" * 200})
    assert "task=" in out
    # 200 chars of `x` would blow past the 100-char cap.
    assert len(out) <= 100


def test_summarize_input_collapses_nested_types(isolated_home: Path) -> None:
    out = summarize_input(
        {"items": [1, 2, 3], "config": {"nested": True}}
    )
    # Nested dicts / lists collapse to their type name to keep the
    # tool-block header readable.
    assert "items=list" in out
    assert "config=dict" in out


# ── app harness ───────────────────────────────────────────────────────


class _StubClient:
    """Minimal ControllerClient stand-in. The push loop reads from
    ``_inbox`` but tests never enqueue anything — we drive ``_render_event``
    directly. Mutation methods record the call for assertions.
    """

    def __init__(self) -> None:
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self.sent: list[dict[str, Any]] = []
        self.shutdown_calls = 0
        self.detach_calls = 0

    async def pushes(self):
        while True:
            payload = await self._inbox.get()
            yield payload

    async def user_input(self, session_id: str, text: str) -> None:
        self.sent.append(
            {"kind": "user_input", "session_id": session_id, "text": text}
        )

    async def approval(
        self,
        session_id: str,
        tool_use_id: str,
        approved: bool,
        operator_note: str | None = None,
    ) -> None:
        self.sent.append(
            {
                "kind": "approval",
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "approved": approved,
            }
        )

    async def detach(self, session_id: str) -> None:
        self.detach_calls += 1

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _new_app(*, replay: list[dict[str, Any]] | None = None) -> TarsApp:
    return TarsApp(
        client=_StubClient(),  # type: ignore[arg-type]
        session_id="sess-test-123",
        shutdown_on_exit=True,
        replay_events=replay or [],
    )


# ── layout ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_mounts_core_layout(isolated_home: Path) -> None:
    app = _new_app()
    async with app.run_test() as pilot:  # noqa: F841 — pilot starts the app
        await pilot.pause()
        # All three primary panes mount.
        assert app.query_one(TranscriptView) is not None
        assert app.query_one(StatusBar) is not None
        assert app.query_one("#input") is not None


# ── per-event rendering ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_text_event_mounts_user_message(
    isolated_home: Path,
) -> None:
    app = _new_app()
    async with app.run_test() as pilot:
        await app._render_event(
            {"kind": "user_text", "text": "hi", "session_id": "sess", "origin": "cli"}
        )
        await pilot.pause()
        messages = app.query(UserMessage)
        assert len(messages) == 1


@pytest.mark.asyncio
async def test_assistant_text_partial_then_finalize(
    isolated_home: Path,
) -> None:
    app = _new_app()
    async with app.run_test() as pilot:
        await app._render_event(
            {
                "kind": "assistant_text",
                "text": "<answer>Hi ",
                "partial": True,
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await pilot.pause()
        # One streaming AssistantMessage in the transcript.
        msgs = app.query(AssistantMessage)
        assert len(msgs) == 1

        await app._render_event(
            {
                "kind": "assistant_text",
                "text": "there</answer>",
                "partial": False,
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await pilot.pause()
        # Same widget, now finalized.
        msgs = app.query(AssistantMessage)
        assert len(msgs) == 1
        widget = msgs.first()
        # Raw buffer holds the streamed text verbatim; rendering strips
        # the persona tags. Check the rendered output via the helper
        # the widget itself uses.
        assert "<answer>" not in strip_persona_tags(widget._buffer)  # type: ignore[attr-defined]
        assert widget._finalized is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_tool_use_mounts_collapsed_tool_block(
    isolated_home: Path,
) -> None:
    app = _new_app()
    async with app.run_test() as pilot:
        await app._render_event(
            {
                "kind": "tool_use",
                "tool": "delegate_claude",
                "input": {"task": "patch auth"},
                "tool_use_id": "call-1",
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await pilot.pause()
        blocks = app.query(ToolBlock)
        assert len(blocks) == 1
        block = blocks.first()
        # Defaults collapsed so the operator's view stays clean.
        assert block.collapsed is True


@pytest.mark.asyncio
async def test_tool_result_failure_auto_expands_block(
    isolated_home: Path,
) -> None:
    app = _new_app()
    async with app.run_test() as pilot:
        await app._render_event(
            {
                "kind": "tool_use",
                "tool": "delegate_claude",
                "input": {"task": "x"},
                "tool_use_id": "call-2",
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await app._render_event(
            {
                "kind": "tool_result",
                "tool_use_id": "call-2",
                "success": False,
                "output": {"error": "boom"},
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await pilot.pause()
        block = app.query(ToolBlock).first()
        # Failed tools auto-expand so the operator sees what went wrong.
        assert block.collapsed is False


@pytest.mark.asyncio
async def test_status_bar_updates_on_tool_lifecycle(
    isolated_home: Path,
) -> None:
    app = _new_app()
    async with app.run_test() as pilot:
        bar = app.query_one(StatusBar)
        # Start idle.
        assert bar._active_tool is None  # type: ignore[attr-defined]
        await app._render_event(
            {
                "kind": "tool_use",
                "tool": "bash",
                "input": {"cmd": "ls"},
                "tool_use_id": "call-3",
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await pilot.pause()
        assert bar._active_tool == "bash"  # type: ignore[attr-defined]
        await app._render_event(
            {
                "kind": "tool_result",
                "tool_use_id": "call-3",
                "success": True,
                "output": {"out": "ok"},
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await pilot.pause()
        assert bar._active_tool is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cli_chunk_appends_to_parent_tool_block(
    isolated_home: Path,
) -> None:
    """cli_chunk events should attach to the matching tool_use_id's
    block, not spawn a standalone widget. Operators see live sub-process
    output inside the tool card."""
    app = _new_app()
    async with app.run_test() as pilot:
        await app._render_event(
            {
                "kind": "tool_use",
                "tool": "delegate_claude",
                "input": {"task": "x"},
                "tool_use_id": "call-4",
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await app._render_event(
            {
                "kind": "cli_chunk",
                "tool": "delegate_claude",
                "tool_use_id": "call-4",
                "phase": "start",
                "text": "",
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await app._render_event(
            {
                "kind": "cli_chunk",
                "tool": "delegate_claude",
                "tool_use_id": "call-4",
                "phase": "chunk",
                "text": "Reading file...\nApplying patch...\n",
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await pilot.pause()
        block = app.query(ToolBlock).first()
        cli_lines = block._cli_lines  # type: ignore[attr-defined]
        assert any("started" in line for line in cli_lines)
        assert any("Reading file" in line for line in cli_lines)
        assert any("Applying patch" in line for line in cli_lines)


@pytest.mark.asyncio
async def test_thinking_indicator_shows_on_send_and_clears_on_reply(
    isolated_home: Path,
) -> None:
    """Sending a message must immediately show a 'thinking' sign (so the
    operator knows it landed + TARS is working); the first reply clears it."""
    from textual.widgets import Input

    app = _new_app()
    async with app.run_test() as pilot:
        inp = app.query_one("#input", Input)
        # Drive the real submit handler with a normal (non-slash) message.
        await app._handle_input(SimpleNamespace(value="hello tars", input=inp))
        await pilot.pause()
        assert len(app.query(ThinkingIndicator)) == 1, "thinking sign should appear on send"

        # The streamed reply clears the indicator.
        await app._render_event(
            {
                "kind": "assistant_text",
                "text": "<answer>hi</answer>",
                "partial": False,
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await pilot.pause()
        assert len(app.query(ThinkingIndicator)) == 0, "reply should clear the thinking sign"


@pytest.mark.asyncio
async def test_thinking_indicator_cleared_when_tool_starts(isolated_home: Path) -> None:
    from textual.widgets import Input

    app = _new_app()
    async with app.run_test() as pilot:
        inp = app.query_one("#input", Input)
        await app._handle_input(SimpleNamespace(value="do a thing", input=inp))
        await pilot.pause()
        assert len(app.query(ThinkingIndicator)) == 1
        await app._render_event(
            {
                "kind": "tool_use",
                "tool": "bash",
                "input": {"cmd": "ls"},
                "tool_use_id": "c1",
                "session_id": "sess",
                "origin": "chat",
            }
        )
        await pilot.pause()
        assert len(app.query(ThinkingIndicator)) == 0


@pytest.mark.asyncio
async def test_input_row_has_prompt_marker(isolated_home: Path) -> None:
    """The flat input frame carries a › prompt marker (Claude-style) and the
    Input keeps id=input so submit/query wiring is unchanged."""
    app = _new_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#input") is not None
        prompt = app.query_one("#input-prompt")
        assert "›" in prompt.render()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_replay_events_render_on_mount(isolated_home: Path) -> None:
    """``replay_events`` from the daemon's attach payload must render
    BEFORE the live push loop starts so the operator sees the prior
    transcript on reattach."""
    replay = [
        {"kind": "user_text", "text": "earlier message", "session_id": "s", "origin": "cli"},
        {
            "kind": "assistant_text",
            "text": "<answer>earlier reply</answer>",
            "partial": False,
            "session_id": "s",
            "origin": "chat",
        },
    ]
    app = _new_app(replay=replay)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.query(UserMessage)) == 1
        assert len(app.query(AssistantMessage)) == 1
