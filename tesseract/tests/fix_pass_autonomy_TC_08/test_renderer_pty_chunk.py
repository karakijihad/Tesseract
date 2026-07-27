"""TC-8 — renderer extension for pty_chunk events.

TC-6 shipped a one-line stub; TC-8 must decode base64 + write the raw
bytes to the renderer stream so a recorded transcript replays as if
the operator had watched the PTY live.
"""

from __future__ import annotations

import base64
import io

from tesseract.orchestrator.tars_controller.renderer import TuiRenderer


def _render(event: dict) -> str:
    buf = io.StringIO()
    renderer = TuiRenderer(stream=buf, color=False)
    renderer.render(event)
    return buf.getvalue()


def test_pty_chunk_renders_decoded_text() -> None:
    data = b"hello pty\n"
    out = _render(
        {
            "kind": "pty_chunk",
            "session_id": "s",
            "ts": "2026-05-23T00:00:00Z",
            "origin": "chat",
            "worker_id": "w-abc",
            "data_b64": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
        }
    )
    assert "hello pty" in out


def test_pty_chunk_preserves_ansi_escapes() -> None:
    payload = b"\x1b[31mred\x1b[0m more"
    out = _render(
        {
            "kind": "pty_chunk",
            "session_id": "s",
            "ts": "2026-05-23T00:00:00Z",
            "origin": "chat",
            "worker_id": "w-abc",
            "data_b64": base64.b64encode(payload).decode("ascii"),
            "encoding": "base64",
        }
    )
    assert "\x1b[31mred" in out


def test_pty_chunk_empty_data_does_not_crash() -> None:
    out = _render(
        {
            "kind": "pty_chunk",
            "session_id": "s",
            "ts": "2026-05-23T00:00:00Z",
            "origin": "chat",
            "worker_id": "w-abc",
            "data_b64": "",
            "encoding": "base64",
        }
    )
    # No newline injected, no exception.
    assert out == ""


def test_pty_chunk_bad_base64_falls_back_to_stub() -> None:
    """If the base64 is malformed the renderer must not crash — emit the
    same one-line stub the TC-6 implementation surfaced."""

    out = _render(
        {
            "kind": "pty_chunk",
            "session_id": "s",
            "ts": "2026-05-23T00:00:00Z",
            "origin": "chat",
            "worker_id": "w-abc",
            "data_b64": "not-base64@@@",
            "encoding": "base64",
        }
    )
    assert "pty" in out and "w-abc"[-8:] in out
