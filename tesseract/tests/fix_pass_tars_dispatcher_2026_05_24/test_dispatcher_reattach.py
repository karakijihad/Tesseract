"""M-3 (Codex audit 2026-05-25) — dispatcher reattach + session-started hook.

`dispatch_to_controller(on_session_started=cb)` exposes the minted session_id
the instant it exists (before the await) so a mission worker can persist it for
restart-resume. `reattach_to_controller(session_id)` rejoins an existing
controller session and recovers its reply — from the transcript replay if the
session already finished, or by tailing live if it is still working.
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
    reattach_to_controller,
)
from tesseract.orchestrator.tars_controller.events import AssistantTextEvent
from tesseract.orchestrator.tars_controller.sessions import SessionRegistry


@pytest.fixture
async def live_daemon(isolated_home: Path) -> AsyncIterator[ControllerDaemon]:
    from tesseract.orchestrator.tars_controller import auth as ctrl_auth

    ctrl_auth.write_token(ctrl_auth.mint_token())
    daemon = ControllerDaemon(
        controller_id="ctrl-test-reattach",
        token=ctrl_auth.read_token() or "",
        registry=SessionRegistry(),
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        yield daemon
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_on_session_started_fires_with_session_id_before_wait(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    """The hook must receive the minted session_id and fire even in the
    fire-and-forget path (it is the worker's only chance to persist the id
    before the awaiting coroutine could be lost to a restart)."""
    seen: list[str] = []

    async def _hook(session_id: str) -> None:
        seen.append(session_id)

    result = await dispatch_to_controller(
        prompt="hello",
        origin="autonomy",
        mode="autonomy",
        wait_for_completion=False,
        on_session_started=_hook,
    )
    assert seen == [result.session_id]


@pytest.mark.asyncio
async def test_on_session_started_failure_is_non_fatal(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    """A failing persistence hook must NOT fail a working dispatch — the
    worst case is the step is not resumable, the same as before M-3."""

    async def _boom(_session_id: str) -> None:
        raise RuntimeError("disk full")

    result = await dispatch_to_controller(
        prompt="hello",
        origin="autonomy",
        mode="autonomy",
        wait_for_completion=False,
        on_session_started=_boom,
    )
    assert isinstance(result, DispatchResult)
    assert result.session_id


@pytest.mark.asyncio
async def test_reattach_recovers_finished_session_from_replay(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    """Session completed (closed assistant_text on the transcript) before we
    reattach — the reply is recovered from the attach replay, no live tail."""
    record = live_daemon._registry.create_session(
        mode="autonomy", origin="autonomy"
    )
    await live_daemon.append_event(
        record.session_id,
        AssistantTextEvent(
            session_id=record.session_id, origin="chat", text="all done.", partial=False
        ),
    )

    result = await reattach_to_controller(
        record.session_id, idle_timeout_seconds=2.0
    )
    assert result.saw_assistant_text is True
    assert result.assistant_text == "all done."
    assert result.metadata.get("reattached") is True


@pytest.mark.asyncio
async def test_reattach_tails_still_running_session(
    isolated_home: Path, live_daemon: ControllerDaemon
) -> None:
    """No closed assistant_text yet at attach time — reattach falls through to
    a live tail and recovers the reply when it lands."""
    record = live_daemon._registry.create_session(
        mode="autonomy", origin="autonomy"
    )

    async def _finish_after(delay: float) -> None:
        await asyncio.sleep(delay)
        await live_daemon.append_event(
            record.session_id,
            AssistantTextEvent(
                session_id=record.session_id, origin="chat", text="late reply.", partial=False
            ),
        )

    finisher = asyncio.create_task(_finish_after(0.2))
    try:
        result = await reattach_to_controller(
            record.session_id, idle_timeout_seconds=3.0
        )
    finally:
        await finisher
    assert result.saw_assistant_text is True
    assert "late reply." in result.assistant_text


@pytest.mark.asyncio
async def test_reattach_raises_when_daemon_down(isolated_home: Path) -> None:
    """No live daemon → reattach must raise DispatcherError (never silently
    fresh-run; the worker maps this to a failed step)."""
    with pytest.raises(DispatcherError):
        await reattach_to_controller("2026-05-25-deadbeef", idle_timeout_seconds=1.0)
