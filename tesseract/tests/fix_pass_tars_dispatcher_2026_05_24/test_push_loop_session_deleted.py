"""``_TuiSession.push_loop`` reacts correctly to ``session_deleted``.

The CLI's push loop has an auto-exit path when an out-of-band delete
arrives for the currently-attached session (Mirror UI, second ``tars``
window): it flips ``keep_daemon_on_exit=True`` (don't kill the daemon —
other sessions might be attached) and ``detach_requested=True`` so the
main loop tears down. A ``session_deleted`` for any OTHER id should
render and continue.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from tesseract.orchestrator.tars_controller.renderer import TuiRenderer
from tesseract.scripts.tars_cli import _TuiSession


class _ScriptedClient:
    """Minimal client whose ``pushes()`` async iterator yields a fixed
    sequence then closes — enough to drive one full push_loop pass."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = list(events)

    async def pushes(self) -> AsyncIterator[dict[str, Any]]:
        for evt in self._events:
            yield evt
        # Terminate the loop cleanly.
        yield {"event": "_disconnected"}


def _make_session(
    events: list[dict[str, Any]], session_id: str
) -> tuple[_TuiSession, io.StringIO]:
    stream = io.StringIO()
    renderer = TuiRenderer(stream=stream, color=False, record=True)
    client = _ScriptedClient(events)
    tui = _TuiSession(client, renderer, session_id)  # type: ignore[arg-type]
    return tui, stream


@pytest.mark.asyncio
async def test_push_loop_exits_on_current_session_deleted(
    isolated_home: Path,
) -> None:
    sid = "2026-05-24-abcdef01"
    tui, _stream = _make_session(
        [{"event": "session_deleted", "session_id": sid}], session_id=sid,
    )

    await asyncio.wait_for(tui.push_loop(), timeout=2.0)

    assert tui.detach_requested is True
    # Critical: don't shut down the daemon — other clients may still be
    # using it. The push_loop must force detach-only semantics.
    assert tui.keep_daemon_on_exit is True


@pytest.mark.asyncio
async def test_push_loop_renders_other_session_deleted_without_exiting(
    isolated_home: Path,
) -> None:
    tui, stream = _make_session(
        [{"event": "session_deleted", "session_id": "2026-05-24-otheriii"}],
        session_id="2026-05-24-currentt",
    )

    await asyncio.wait_for(tui.push_loop(), timeout=2.0)

    # The disconnected sentinel at the end of the scripted list is
    # what flips ``detach_requested`` — for our purposes the important
    # contract is that ``keep_daemon_on_exit`` stayed ``None`` (CLI
    # default applies) because the deleted session was not ours.
    assert tui.keep_daemon_on_exit is None
    rendered = tui.renderer.recorded_text() + stream.getvalue()
    assert "2026-05-24-otheriii" in rendered


@pytest.mark.asyncio
async def test_push_loop_renders_session_renamed(isolated_home: Path) -> None:
    tui, stream = _make_session(
        [
            {
                "event": "session_renamed",
                "session_id": "2026-05-24-currentt",
                "title": "auth refactor",
            }
        ],
        session_id="2026-05-24-currentt",
    )

    await asyncio.wait_for(tui.push_loop(), timeout=2.0)

    rendered = tui.renderer.recorded_text() + stream.getvalue()
    assert "auth refactor" in rendered
    # Rename of the current session must NOT trigger auto-exit.
    assert tui.keep_daemon_on_exit is None
