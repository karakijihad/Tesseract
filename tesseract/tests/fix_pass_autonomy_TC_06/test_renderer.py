"""TC-6 — TuiRenderer output assertions.

One test per event kind from `_shared/transcript-events.md` plus the
TC-5 push events (`session_status`, `reload_complete`) and the
unknown-kind safety net.
"""

from __future__ import annotations

import io

import pytest

from tesseract.orchestrator.tars_controller.renderer import (
    HEADER,
    TuiRenderer,
)


def _renderer(*, color: bool = False) -> tuple[TuiRenderer, io.StringIO]:
    buf = io.StringIO()
    return TuiRenderer(stream=buf, color=color), buf


# ── header ────────────────────────────────────────────────────────────


def test_header_includes_identity_and_session_id():
    r, buf = _renderer()
    r.render_header(session_id="2026-05-23-deadbeef")
    out = buf.getvalue()
    assert "TESSERACT" in out
    assert "TARS" in out
    assert "2026-05-23-deadbeef" in out


def test_header_without_session_id_only_renders_box():
    r, buf = _renderer()
    r.render_header()
    out = buf.getvalue()
    assert HEADER in out
    assert "session:" not in out


# ── per-kind rendering ────────────────────────────────────────────────


def test_user_text_renders_with_prefix():
    r, buf = _renderer()
    r.render({"kind": "user_text", "text": "hello"})
    out = buf.getvalue()
    # 2026-05-24 — rich-based renderer uses ``›`` prefix (the operator's
    # own input is already visible in the prompt_toolkit input box;
    # this prefix is for transcript replay + observer windows).
    assert "›" in out
    assert "hello" in out


def test_assistant_text_streams_partial_then_flushes():
    r, buf = _renderer()
    r.render({"kind": "assistant_text", "text": "Hi ", "partial": True})
    mid = buf.getvalue()
    assert "▍tars" in mid
    assert "Hi " in mid
    r.render({"kind": "assistant_text", "text": "there.", "partial": False})
    out = buf.getvalue()
    assert "Hi there." in out
    # A subsequent assistant chunk gets its own header.
    r.render({"kind": "assistant_text", "text": "Next.", "partial": False})
    after = buf.getvalue()
    assert after.count("▍tars") == 2


def test_assistant_streaming_flushed_by_non_assistant_event():
    """If a tool_use arrives mid-stream, the partial assistant line
    must be terminated first so the next line starts cleanly."""
    r, buf = _renderer()
    r.render({"kind": "assistant_text", "text": "thinking…", "partial": True})
    r.render(
        {"kind": "tool_use", "tool": "delegate_tars_controller", "input": {}}
    )
    out = buf.getvalue()
    lines = out.splitlines()
    assert any("▍tars" in line for line in lines)
    # 2026-05-24 — tool_use line now uses the Claude-CLI ``●`` bullet
    # instead of the old ``↳ tool ·`` prefix.
    assert any("●" in line for line in lines)


def test_tool_use_renders_summary():
    r, buf = _renderer()
    r.render(
        {
            "kind": "tool_use",
            "tool": "bash",
            "input": {"cmd": "ls -la"},
            "tool_use_id": "tu-1",
        }
    )
    out = buf.getvalue()
    assert "●" in out
    assert "bash" in out
    assert "cmd=" in out


def test_tool_result_success_and_failure():
    r, buf = _renderer()
    r.render({"kind": "tool_result", "success": True, "output": {"ok": True}})
    out_ok = buf.getvalue()
    # 2026-05-24 — success now reads ``● done`` (Claude-CLI vocabulary)
    # instead of the old ``✓`` mark.
    assert "● done" in out_ok
    r, buf = _renderer()
    r.render(
        {
            "kind": "tool_result",
            "success": False,
            "timed_out": True,
            "output": {"err": "timeout"},
        }
    )
    out_fail = buf.getvalue()
    # 2026-05-24 — timed_out failures render as ``● timed out`` (the
    # status word is the human-readable form, not the raw kwarg).
    assert "● timed out" in out_fail or "● failed" in out_fail
    assert "timeout" in out_fail


def test_permission_request_with_resolution():
    r, buf = _renderer()
    r.render(
        {
            "kind": "permission_request",
            "tool": "bash",
            "summary": "rm -rf /tmp/foo",
            "posture": "ask",
            "resolved": True,
            "resolution": "headless_blocked",
        }
    )
    out = buf.getvalue()
    assert "‼ permission" in out
    assert "bash" in out
    assert "headless_blocked" in out


def test_worker_status_with_progress():
    r, buf = _renderer()
    r.render(
        {
            "kind": "worker_status",
            "worker_id": "wk-abcdef-12345678",
            "worker_kind": "claude_cli",
            "status": "running",
            "progress": "2/5 files",
        }
    )
    out = buf.getvalue()
    assert "· worker claude_cli" in out
    assert "12345678" in out  # tail of worker id
    assert "2/5 files" in out


def test_artifact_renders_kind_and_path():
    r, buf = _renderer()
    r.render(
        {
            "kind": "artifact",
            "worker_id": "wk-1",
            "artifact_type": "patch",
            "path": "/tmp/artifact.patch",
        }
    )
    out = buf.getvalue()
    assert "+ artifact patch" in out
    assert "/tmp/artifact.patch" in out


def test_child_transcript_ref():
    r, buf = _renderer()
    r.render(
        {
            "kind": "child_transcript_ref",
            "child_session_id": "2026-05-23-cafef00d",
            "child_transcript_path": "/x/y/z.jsonl",
            "worker_id": "wk-parent",
        }
    )
    out = buf.getvalue()
    assert "→ child" in out
    assert "cafef00d" in out


def test_journal_entry_renders_type():
    r, buf = _renderer()
    r.render({"kind": "journal_entry", "entry_type": "follow_up_draft"})
    out = buf.getvalue()
    assert "· journal follow_up_draft" in out


def test_pty_chunk_decodes_to_raw_output():
    # TC-8 upgraded the renderer: base64 decodes to raw bytes written
    # straight to the stream (no prefix). The TC-6 stub remains as a
    # fallback for malformed payloads.
    r, buf = _renderer()
    r.render(
        {"kind": "pty_chunk", "worker_id": "wk-pty", "data_b64": "SGVsbG8="}
    )
    out = buf.getvalue()
    assert "Hello" in out


def test_unknown_kind_renders_safe_stub():
    r, buf = _renderer()
    r.render({"kind": "future_kind_42"})
    out = buf.getvalue()
    assert "unknown event" in out
    assert "future_kind_42" in out


# ── lifecycle pushes (TC-5) ───────────────────────────────────────────


def test_session_status_with_reason():
    r, buf = _renderer()
    r.render_session_status(
        {"session_id": "x", "status": "idle", "reason": "reload:all"}
    )
    out = buf.getvalue()
    assert "· session idle" in out
    assert "reload:all" in out


def test_reload_complete_with_failures():
    r, buf = _renderer()
    r.render_reload_complete(
        {
            "target": "all",
            "reloaded": ["adapter", "tool_registry"],
            "failed": ["system_prompt: boom"],
            "pending_turns": 1,
        }
    )
    out = buf.getvalue()
    assert "⟳ reload all" in out
    assert "adapter, tool_registry" in out
    assert "pending turns: 1" in out
    assert "system_prompt: boom" in out


# ── ANSI toggle ───────────────────────────────────────────────────────


def test_no_color_omits_escape_codes():
    r, buf = _renderer(color=False)
    r.render({"kind": "user_text", "text": "hello"})
    out = buf.getvalue()
    assert "\x1b[" not in out


def test_color_enabled_emits_escape_codes():
    r, buf = _renderer(color=True)
    r.render({"kind": "user_text", "text": "hello"})
    out = buf.getvalue()
    assert "\x1b[" in out
