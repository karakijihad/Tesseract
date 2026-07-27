"""Audit-3 M3 — verify the controller cli_sink reads the canonical
``delta`` key emitted by ``run_subprocess_with_sink``.

Before the fix the sink read ``payload.get("text") or payload.get("output")``
but the producer only ever set ``delta``, so every cli_chunk arrived
with empty text.
"""

from __future__ import annotations

from typing import Any

import pytest

from tesseract.scripts.tars_controller import _make_controller_cli_sink


class _FakeDaemon:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append_event(self, session_id: str, event: Any) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_sink_reads_delta_key() -> None:
    daemon = _FakeDaemon()
    sink = _make_controller_cli_sink(daemon, "sess-1")
    await sink(
        "cli_output",
        "call-7",
        {"tool": "delegate_claude", "delta": "hello world"},
    )
    assert len(daemon.events) == 1
    evt = daemon.events[0]
    assert evt.kind == "cli_chunk"
    assert evt.text == "hello world"
    assert evt.tool == "delegate_claude"
    assert evt.tool_use_id == "call-7"
    assert evt.phase == "chunk"


@pytest.mark.asyncio
async def test_sink_falls_back_to_legacy_text_key() -> None:
    """Belt-and-braces: a third-party sink that ignores the typed
    payload and emits ``text`` should still surface SOMETHING in the
    transcript rather than an empty bubble."""
    daemon = _FakeDaemon()
    sink = _make_controller_cli_sink(daemon, "sess-2")
    await sink(
        "cli_output",
        "call-8",
        {"tool": "delegate_codex", "text": "legacy chunk"},
    )
    assert daemon.events[0].text == "legacy chunk"


@pytest.mark.asyncio
async def test_sink_start_and_end_phases() -> None:
    daemon = _FakeDaemon()
    sink = _make_controller_cli_sink(daemon, "sess-3")
    await sink("cli_start", "c1", {"tool": "delegate_claude"})
    await sink("cli_end", "c1", {"tool": "delegate_claude", "exit_code": 0})
    assert [e.phase for e in daemon.events] == ["start", "end"]
    assert daemon.events[-1].exit_code == 0
    # Start/end events have empty text — only chunk-phase carries data.
    assert daemon.events[0].text == ""
