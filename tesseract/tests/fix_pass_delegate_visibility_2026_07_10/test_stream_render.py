"""Fix 3 — ClaudeDelegateStreamParser renders stream-json events live and
extracts the final result text (delegate visibility fix-pass 2026-07-10)."""

from __future__ import annotations

import json

from tesseract.kernel.tools.claude_stream_render import ClaudeDelegateStreamParser


def _event(**kw) -> str:
    return json.dumps(kw) + "\n"


def test_renders_init_tool_use_and_result():
    p = ClaudeDelegateStreamParser()
    out = p.feed(_event(type="system", subtype="init", model="test-model"))
    assert "session started" in out and "test-model" in out
    out = p.feed(_event(
        type="assistant",
        message={"content": [
            {"type": "text", "text": "Working on it."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.js"}},
        ]},
    ))
    assert "Working on it." in out
    assert "→ Edit" in out and "a.js" in out
    out = p.feed(_event(type="result", subtype="success", result="all done", duration_ms=3000))
    assert "finished: success" in out
    assert p.final_output() == "all done"
    assert not p.is_error


def test_partial_lines_across_chunks():
    p = ClaudeDelegateStreamParser()
    line = _event(type="result", subtype="success", result="split ok")
    assert p.feed(line[:10]) == ""
    out = p.feed(line[10:])
    assert "finished" in out
    assert p.final_output() == "split ok"


def test_unparseable_line_passes_through():
    p = ClaudeDelegateStreamParser()
    out = p.feed("not json at all\n")
    assert "not json at all" in out


def test_tool_result_echo_is_silent():
    p = ClaudeDelegateStreamParser()
    out = p.feed(_event(
        type="user",
        message={"content": [{"type": "tool_result", "content": "big blob"}]},
    ))
    assert out == ""


def test_no_result_event_final_output_is_none():
    p = ClaudeDelegateStreamParser()
    p.feed(_event(type="assistant", message={"content": [{"type": "text", "text": "hi"}]}))
    assert p.final_output() is None


def test_flush_consumes_trailing_partial_line():
    p = ClaudeDelegateStreamParser()
    p.feed(json.dumps({"type": "result", "subtype": "success", "result": "tail"}))
    assert p.final_output() is None
    p.flush()
    assert p.final_output() == "tail"


def test_error_result_flagged():
    p = ClaudeDelegateStreamParser()
    p.feed(_event(type="result", subtype="error_during_execution", result="boom", is_error=True))
    assert p.is_error
    assert p.final_output() == "boom"
