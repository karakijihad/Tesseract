"""Shared dispatcher — end-to-end against a live in-process daemon.

These tests spin up a real :class:`ControllerDaemon` on a tmp port, so
the dispatcher exercises the actual IPC handshake / session-mint /
``user_input`` / transcript fan-out path. The daemon is constructed
WITHOUT a ``dispatch_turn`` callback so ``user_input`` is persisted as
a ``user_text`` event and ack'd, but no real chat brain fires — the
test then manually emits an ``assistant_text`` event via
``daemon.append_event`` to satisfy ``tail_until_assistant_text``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from tesseract.orchestrator.tars_controller.daemon import ControllerDaemon
from tesseract.orchestrator.tars_controller.dispatcher import (
    DispatchResult,
    DispatcherError,
    dispatch_to_controller,
    ensure_daemon_running,
)
from tesseract.orchestrator.tars_controller.events import AssistantTextEvent
from tesseract.orchestrator.tars_controller.sessions import SessionRegistry


@pytest.fixture
async def live_daemon(isolated_home: Path) -> AsyncIterator[ControllerDaemon]:
    # Token must exist on disk for the dispatcher's ControllerClient
    # connect to succeed.
    from tesseract.orchestrator.tars_controller import auth as ctrl_auth

    ctrl_auth.write_token(ctrl_auth.mint_token())
    daemon = ControllerDaemon(
        controller_id="ctrl-test-dispatcher",
        token=ctrl_auth.read_token() or "",
        registry=SessionRegistry(),
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        yield daemon
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_ensure_daemon_running_returns_true_when_alive(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    assert await ensure_daemon_running() is True


@pytest.mark.asyncio
async def test_ensure_daemon_running_returns_false_when_spawn_disabled(
    isolated_home: Path,
) -> None:
    # No live daemon, spawn explicitly disabled → False, no exception.
    assert (
        await ensure_daemon_running(spawn_if_missing=False) is False
    )


@pytest.mark.asyncio
async def test_dispatch_to_controller_fire_and_forget(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    """``wait_for_completion=False`` returns the session_id immediately
    without tailing — the chat-side hand-off pattern."""
    result = await dispatch_to_controller(
        prompt="hello world",
        origin="mirror",
        title="test-session",
        mode="chat",
        wait_for_completion=False,
    )
    assert isinstance(result, DispatchResult)
    assert result.session_id.startswith(
        # YYYY-MM-DD-<hex> from mint_session_id
        ""
    )
    assert result.assistant_text == ""
    assert result.saw_assistant_text is False
    assert result.metadata.get("detached") is True
    # Session was actually minted on disk.
    sessions = live_daemon._registry.list_sessions()
    assert any(s.session_id == result.session_id for s in sessions)


@pytest.mark.asyncio
async def test_dispatch_to_controller_waits_for_assistant_text(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    """``wait_for_completion=True`` tails until a closed assistant_text
    event arrives, then returns the accumulated text."""
    # Pre-arrange: a background task that injects the assistant_text
    # event ~50ms after dispatch starts so the tail observes it.
    async def _inject_after(delay: float, text: str) -> None:
        await asyncio.sleep(delay)
        # The dispatcher mints sessions by calling new_session which
        # persists on disk. Read the most recent session id from the
        # registry instead of guessing.
        sessions = live_daemon._registry.list_sessions()
        assert sessions, "expected dispatcher to have minted a session"
        latest = max(sessions, key=lambda s: s.created_at)
        await live_daemon.append_event(
            latest.session_id,
            AssistantTextEvent(
                session_id=latest.session_id,
                origin="chat",
                text=text,
                partial=False,
            ),
        )

    injector = asyncio.create_task(_inject_after(0.2, "done."))
    try:
        result = await dispatch_to_controller(
            prompt="please summarize",
            origin="autonomy",
            title=None,
            mode="autonomy",
            wait_for_completion=True,
            idle_timeout_seconds=3.0,
        )
    finally:
        await injector

    assert result.saw_assistant_text is True
    assert result.assistant_text == "done."
    assert result.success is True


@pytest.mark.asyncio
async def test_dispatch_to_controller_times_out_when_no_assistant_text(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    """Without an assistant_text event the dispatcher must surface
    ``timed_out=True`` within the configured idle budget — not hang
    forever on the inbox queue."""
    result = await dispatch_to_controller(
        prompt="hello",
        origin="autonomy",
        mode="autonomy",
        wait_for_completion=True,
        idle_timeout_seconds=0.5,
    )
    assert result.timed_out is True
    assert result.saw_assistant_text is False
    assert result.success is False


@pytest.mark.asyncio
async def test_dispatch_without_title_derives_goal_label(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    """Task 6.1 — no ``title`` given must still yield an operator-meaningful
    ActivityRecord label. Before the fix, ``sessions.py::create_session``
    fell back to the bare ``mode`` string (``label=record.title or
    record.mode``) whenever ``title`` was omitted."""
    from tesseract.orchestrator.activity import get_activity_registry

    result = await dispatch_to_controller(
        prompt="investigate the flaky auth test",
        origin="mirror",
        mode="chat",
        wait_for_completion=False,
    )
    rec = get_activity_registry().get(f"session:{result.session_id}")
    assert rec is not None
    assert rec.label == "investigate the flaky auth test"


@pytest.mark.asyncio
async def test_dispatch_to_controller_rejects_empty_prompt(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    with pytest.raises(DispatcherError):
        await dispatch_to_controller(
            prompt="",
            origin="mirror",
            wait_for_completion=False,
        )
    with pytest.raises(DispatcherError):
        await dispatch_to_controller(
            prompt="   \n\t",
            origin="mirror",
            wait_for_completion=False,
        )


@pytest.mark.asyncio
async def test_cancel_event_fires_within_one_loop_turn_during_idle(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    """Reviewer Bug 2 lock: a cancel arriving while the dispatcher is
    blocked on inbox.get() must be honored WITHOUT waiting for
    ``idle_timeout_seconds``. Earlier code only checked
    ``cancel_event.is_set()`` at the top of the loop, so a cancel
    could sit pending for up to 5 minutes (default idle timeout).
    """
    cancel_event = asyncio.Event()

    async def _trip_cancel_after(delay: float) -> None:
        await asyncio.sleep(delay)
        cancel_event.set()

    canceller = asyncio.create_task(_trip_cancel_after(0.2))
    started = asyncio.get_running_loop().time()
    try:
        result = await dispatch_to_controller(
            prompt="hold the line",
            origin="autonomy",
            mode="autonomy",
            wait_for_completion=True,
            # Generous idle timeout so the elapsed time below ONLY
            # reflects cancel-responsiveness, not the timeout path.
            idle_timeout_seconds=30.0,
            cancel_event=cancel_event,
        )
    finally:
        await canceller

    elapsed = asyncio.get_running_loop().time() - started
    assert result.cancelled is True, (
        f"cancel must be observed during idle wait, got {result}"
    )
    assert elapsed < 2.0, (
        f"cancel took {elapsed:.2f}s — must fire within one loop turn, "
        "not wait for idle_timeout_seconds"
    )


@pytest.mark.asyncio
async def test_concurrent_ensure_daemon_running_spawns_only_one(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer Bug 3 lock: two concurrent ``ensure_daemon_running``
    calls with no live daemon must share ONE spawn — the spawn-lock
    file under ``<TESSERACT_HOME>/run/`` ensures the second caller
    polls instead of starting a parallel daemon.

    We monkeypatch the ``_spawn_daemon_subprocess`` helper so the test
    doesn't actually fork a real controller; the polling loop is
    short-circuited by also patching ``_is_daemon_alive`` to return
    ``True`` immediately after the (one) recorded spawn.
    """
    from tesseract.orchestrator.tars_controller import dispatcher as dispatcher_mod

    spawn_calls = {"n": 0}
    alive_after_spawn = asyncio.Event()

    def _fake_spawn() -> object:
        spawn_calls["n"] += 1
        # Mimic a daemon writing its port file then becoming alive.
        alive_after_spawn.set()

        class _FakeProc:
            pid = 12345

        return _FakeProc()

    def _fake_is_alive(timeout: float = 0.5) -> bool:
        return alive_after_spawn.is_set()

    monkeypatch.setattr(
        dispatcher_mod, "_spawn_daemon_subprocess", _fake_spawn
    )
    monkeypatch.setattr(dispatcher_mod, "_is_daemon_alive", _fake_is_alive)

    results = await asyncio.gather(
        dispatcher_mod.ensure_daemon_running(),
        dispatcher_mod.ensure_daemon_running(),
        dispatcher_mod.ensure_daemon_running(),
    )
    assert all(results)
    assert spawn_calls["n"] == 1, (
        f"expected one spawn under lock, got {spawn_calls['n']}"
    )
