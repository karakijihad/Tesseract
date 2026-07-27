"""Tests for the 2026-05-24 live-testing follow-up fixes:
OSC-stripping, the IPC reply demultiplexer (/sessions race), and the
background-spawn rail linkage.
"""

from __future__ import annotations

import asyncio

import pytest

from tesseract.kernel.tools.cli_stream import _strip_control_sequences


# ── OSC / control-sequence stripping (terminal-rename fix) ────────────


def test_strip_osc_title_sequence() -> None:
    # claude/codex emit ESC]0;<title>BEL to set the terminal title.
    raw = "\x1b]0;claude\x07hello world"
    assert _strip_control_sequences(raw) == "hello world"


def test_strip_osc_st_terminated() -> None:
    raw = "\x1b]2;some title\x1b\\actual output"
    assert _strip_control_sequences(raw) == "actual output"


def test_strip_sgr_colour() -> None:
    raw = "\x1b[31mred\x1b[0m text"
    assert _strip_control_sequences(raw) == "red text"


def test_strip_csi_cursor_moves() -> None:
    raw = "line1\x1b[2K\x1b[1Gline2"
    out = _strip_control_sequences(raw)
    assert "line1" in out and "line2" in out
    assert "\x1b" not in out


def test_strip_leaves_plain_text_and_newlines() -> None:
    raw = "plain\ntext\nhere"
    assert _strip_control_sequences(raw) == "plain\ntext\nhere"


# ── IPC reply demultiplexer (/sessions race) ──────────────────────────


class _FakeReader:
    """Feeds pre-scripted JSON lines, then blocks forever."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        await asyncio.sleep(3600)
        return b""


class _FakeWriter:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@pytest.mark.asyncio
async def test_reply_demux_survives_concurrent_pushes_consumer() -> None:
    """The core /sessions bug: a permanent pushes() consumer used to eat
    request/reply payloads off the shared inbox. With the demuxer, an
    _await_event call resolves even while pushes() is draining."""
    from tesseract.orchestrator.tars_controller.ipc_client import (
        ControllerClient,
    )

    # Reader will deliver: a transcript push (goes to pushes()), then a
    # session_list reply (must reach _await_event, not the push loop).
    import json

    lines = [
        (json.dumps({"event": "transcript_event", "transcript_event": {"kind": "user_text"}}) + "\n").encode(),
        (json.dumps({"event": "session_list", "sessions": [{"session_id": "s-1"}]}) + "\n").encode(),
    ]
    client = ControllerClient(_FakeReader(lines), _FakeWriter(), "tok")  # type: ignore[arg-type]
    client._start_reader_task()

    pushes_seen: list[dict] = []

    async def _drain_pushes() -> None:
        async for push in client.pushes():
            if push.get("event") == "_disconnected":
                return
            pushes_seen.append(push)

    push_task = asyncio.create_task(_drain_pushes())
    # Give the reader a moment to enqueue, then issue the request/reply.
    sessions = await client.list_sessions()
    assert sessions == [{"session_id": "s-1"}]
    # The transcript push still reached the pushes() consumer.
    await asyncio.sleep(0.05)
    assert any(p.get("event") == "transcript_event" for p in pushes_seen)
    push_task.cancel()
    try:
        await push_task
    except asyncio.CancelledError:
        pass
    await client.close()


# ── background-spawn rail linkage ─────────────────────────────────────


@pytest.mark.asyncio
async def test_await_or_error_requeues_unrelated_error_on_success() -> None:
    """Reviewer-flagged race: if an out-of-band ``error`` push lands in
    the same wait tick as the success reply, it must be handed back to
    the push loop, not swallowed by the error waiter."""
    import json

    from tesseract.orchestrator.tars_controller.ipc_client import (
        ControllerClient,
    )

    # Both a success reply AND an unrelated error arrive back-to-back.
    lines = [
        (json.dumps({"event": "session_deleted", "session_id": "sid"}) + "\n").encode(),
        (json.dumps({"event": "error", "code": "unrelated", "detail": "later"}) + "\n").encode(),
    ]
    client = ControllerClient(_FakeReader(lines), _FakeWriter(), "tok")  # type: ignore[arg-type]
    client._start_reader_task()
    # Let both lines be read + routed before we inspect.
    result = await client._await_event_or_error("session_deleted")
    assert result["session_id"] == "sid"
    # The unrelated error must be retrievable from the inbox (push loop).
    await asyncio.sleep(0.05)
    seen = []
    while not client._inbox.empty():
        seen.append(client._inbox.get_nowait())
    assert any(p.get("event") == "error" and p.get("code") == "unrelated" for p in seen)
    await client.close()


def test_spawn_handle_regex_extracts_handle() -> None:
    from tesseract.scripts.tars_app import _SPAWN_HANDLE_RE

    text = (
        "delegate_claude spawned in background: "
        "handle=del-claude-20260524-182432-125763. Use spawn_check…"
    )
    m = _SPAWN_HANDLE_RE.search(text)
    assert m is not None
    assert m.group(1) == "del-claude-20260524-182432-125763"
