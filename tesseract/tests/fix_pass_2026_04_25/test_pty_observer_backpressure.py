"""P15-K: per-pane observer-push backpressure cap.

A high-throughput PTY would otherwise pile up `asyncio.Task`s faster
than the observer can consume them. The cap (default
OBSERVER_PUSH_CAP_PER_PANE = 16) drops the oldest in-flight push and
logs a warning instead of letting the queue grow unbounded.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

from aiohttp import web

from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
from tesseract.mirror.server.pty_manager import (
    OBSERVER_PUSH_CAP_PER_PANE,
    PTYEntry,
    PTYManager,
)


class _SlowObserver:
    """Observer whose `observe_incremental` never returns until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def observe_incremental(self, **_kwargs) -> None:  # noqa: ANN001
        self.calls += 1
        await self.release.wait()


def _build_pty_with_observer(observer) -> tuple[PTYManager, web.Application]:
    cfg = TerminalServerConfig(
        default_shell="bash",
        max_tabs=1,
        max_panes_per_tab=4,
        shell_profiles={"bash": ShellProfile(argv=("bash",), label="bash")},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=2_000_000,
    )
    pty = PTYManager(cfg)
    app = web.Application()
    app["observer"] = observer
    app["observer_state"] = "observing"
    app["observer_consented_panes"] = {"p1"}
    pty.bind_app(app)
    return pty, app


def _stub_entry(pty: PTYManager, pane_id: str) -> PTYEntry:
    proc = MagicMock()
    proc.isalive.return_value = True
    proc.write = MagicMock(return_value=None)
    entry = PTYEntry(pane_id=pane_id, shell="bash", proc=proc, ws=MagicMock(), owner="user")
    pty._ptys[pane_id] = entry
    return entry


async def test_observer_push_cap_drops_oldest_in_flight(caplog):
    observer = _SlowObserver()
    pty, app = _build_pty_with_observer(observer)
    entry = _stub_entry(pty, "p1")

    caplog.set_level(logging.WARNING, logger="tesseract.mirror.server.pty_manager")

    # Fire CAP+5 chunks. The observer never releases, so all pushes pile up.
    for i in range(OBSERVER_PUSH_CAP_PER_PANE + 5):
        pty._forward_to_observer("p1", f"line {i}\n")
        # Yield once per push so the spawned task actually starts running.
        await asyncio.sleep(0)

    # The per-pane queue is bounded, regardless of how many we fired.
    assert len(entry.observer_tasks) <= OBSERVER_PUSH_CAP_PER_PANE
    # And the warning was emitted for the dropped overflow.
    assert any(
        "observer-push cap" in rec.message for rec in caplog.records
    ), [r.message for r in caplog.records]

    # Cleanup: release the slow observer and let in-flight tasks finish/cancel.
    observer.release.set()
    for t in list(entry.observer_tasks):
        try:
            await asyncio.wait_for(t, timeout=0.2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


async def test_observer_push_no_entry_still_tracked_app_level():
    """Test paths fire `_forward_to_observer` without a real pane. The cap
    skips when no PTYEntry exists, but app-level tracking still happens so
    disarm can cancel the in-flight push."""
    observer = _SlowObserver()
    pty, app = _build_pty_with_observer(observer)
    # Note: NO entry created via _stub_entry.

    pty._forward_to_observer("p1", "hello\n")
    await asyncio.sleep(0)

    tracked = app.get("observer_pty_tasks")
    assert tracked is not None
    assert len(tracked) == 1

    observer.release.set()
    await asyncio.sleep(0.05)
    assert len(app["observer_pty_tasks"]) == 0


async def test_observer_push_skipped_when_pane_not_consented():
    observer = _SlowObserver()
    pty, app = _build_pty_with_observer(observer)
    _stub_entry(pty, "p1")
    app["observer_consented_panes"] = set()  # revoke consent

    pty._forward_to_observer("p1", "secret\n")
    await asyncio.sleep(0)

    assert observer.calls == 0
    assert len(app.get("observer_pty_tasks", set())) == 0
