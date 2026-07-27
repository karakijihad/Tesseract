"""Fix 3 — run_subprocess_with_sink drives the output parser: rendered deltas
reach the sink live, the result event supplies the ToolResult text, and a
timeout carries the transcript tail (fix-pass 2026-07-10)."""

from __future__ import annotations

import asyncio
import json
import sys

from tesseract.kernel.tools.claude_stream_render import ClaudeDelegateStreamParser
from tesseract.kernel.tools.cli_stream import run_subprocess_with_sink

_FAKE_STREAM = [
    {"type": "system", "subtype": "init", "model": "fake-model"},
    {
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "Editing the file now."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.js"}},
        ]},
    },
    {"type": "result", "subtype": "success", "result": "FINAL ANSWER", "duration_ms": 1200},
]


def _emit_script(events, sleep_after: float = 0) -> str:
    payload = json.dumps(events)
    return (
        "import json, sys, time; "
        f"[print(json.dumps(e), flush=True) for e in json.loads({payload!r})]; "
        f"time.sleep({sleep_after})"
    )


def _run(argv, timeout, sink):
    return asyncio.run(
        run_subprocess_with_sink(
            tool_name="delegate_claude",
            argv=argv,
            cwd=".",
            timeout=timeout,
            sink=sink,
            call_id="call-1",
            empty_message="empty",
            missing_message="missing",
            output_parser=ClaudeDelegateStreamParser(),
        )
    )


def test_result_event_becomes_tool_output_and_deltas_are_rendered():
    deltas = []

    async def sink(kind, call_id, payload):
        if kind == "cli_output":
            deltas.append(payload["delta"])

    result = _run(
        (sys.executable, "-c", _emit_script(_FAKE_STREAM)), timeout=30, sink=sink
    )
    assert not result.is_error
    assert result.output == "FINAL ANSWER"
    joined = "".join(deltas)
    assert "session started" in joined and "fake-model" in joined
    assert "Editing the file now." in joined
    assert "→ Edit" in joined
    # Raw NDJSON must NOT reach the sink.
    assert '"type"' not in joined


def test_timeout_returns_partial_transcript_tail():
    async def sink(kind, call_id, payload):
        pass

    # Emits two events then sleeps past the timeout — no result event.
    result = _run(
        (sys.executable, "-c", _emit_script(_FAKE_STREAM[:2], sleep_after=30)),
        timeout=3,
        sink=sink,
    )
    assert result.is_error and result.timed_out
    assert "timed out" in result.output
    assert "Transcript tail" in result.output
    assert "Editing the file now." in result.output


def test_exit_zero_with_error_result_event_is_an_error():
    """claude can exit 0 while the turn failed (error_max_turns etc.) —
    the parser's turn-level verdict must reach ToolResult.is_error."""
    async def sink(kind, call_id, payload):
        pass

    events = [
        {"type": "result", "subtype": "error_max_turns", "result": "ran out of turns", "is_error": True},
    ]
    result = _run(
        (sys.executable, "-c", _emit_script(events)), timeout=30, sink=sink
    )
    assert result.is_error
    assert "ran out of turns" in result.output


def test_without_parser_behavior_unchanged():
    async def sink(kind, call_id, payload):
        pass

    result = asyncio.run(
        run_subprocess_with_sink(
            tool_name="delegate_codex",
            argv=(sys.executable, "-c", "print('plain text out')"),
            cwd=".",
            timeout=30,
            sink=sink,
            call_id="call-2",
            empty_message="empty",
            missing_message="missing",
        )
    )
    assert not result.is_error
    assert result.output == "plain text out"
