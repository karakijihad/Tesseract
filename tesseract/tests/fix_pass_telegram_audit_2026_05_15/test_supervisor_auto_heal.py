"""Regression — bridge supervisor self-heals and Restart works after stop().

Two real-world bugs the operator hit on 2026-05-16:

1. ``getMe`` failing at boot left the bridge permanently disabled — the
   only way to recover was a manual Restart, which was itself broken
   (see below).
2. ``stop()`` set ``_stop_event`` and never reset it, so the next
   ``start()`` call exited the supervised loop immediately. Restart
   button silently no-op'd, then the ASK gate returned "operator
   declined" because the cockpit WS had timed out → user saw
   "tool denied" with no actionable signal.

This test pins:
- ``getMe`` retried indefinitely with bounded backoff (first attempt
  fails, second succeeds → bridge transitions to running).
- ``stop()`` resets the stop event so a follow-up ``start()`` runs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.api import TelegramAPIError
from tesseract.integrations.telegram.bridge import TelegramBridge
from tesseract.integrations.telegram.state import StateBundle


def _new_bridge(tmp_path, monkeypatch, *, api_mock) -> TelegramBridge:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._token = "fake"
    bridge._app = MagicMock()
    bridge._app.get = lambda k, default=None: default
    bridge._conversations = MagicMock()
    bridge._sessions = {}
    bridge._retention = RetentionPolicy.fallback()
    bridge._poll_task = None
    bridge._stop_event = asyncio.Event()
    bridge._started_at = ""
    bridge._last_poll_at = None
    bridge._error_count = 0
    bridge._bridge_phase = "stopped"
    bridge._last_getme_error = None
    state_dir = tmp_path / "telegram"
    state_dir.mkdir(parents=True, exist_ok=True)
    bridge._state = StateBundle(dir_path=state_dir)
    bridge._api = api_mock
    return bridge


def _patch_telegram_api(monkeypatch, api) -> None:
    """Make ``TelegramAPI(token)`` return our pre-built mock so the
    supervised loop calls our flaky / OK ``get_me`` instead of trying
    to hit api.telegram.org with the fake token from the fixture."""
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge.TelegramAPI",
        lambda _token: api,
    )


@pytest.mark.asyncio
async def test_supervisor_retries_getme_until_success(tmp_path, monkeypatch) -> None:
    """First getMe attempt fails (network blip); second succeeds —
    bridge transitions stopped → starting → running on its own.

    The supervised loop's poll path is stubbed to a sleep-forever so
    the test isolates the getMe-retry behaviour from poll-loop state
    persistence (Windows file-lock races on tmp_path).
    """
    calls: list[int] = []

    async def flaky_get_me() -> dict[str, object]:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise TelegramAPIError("simulated tls handshake failure")
        return {"username": "test_bot"}

    api = MagicMock()
    api.get_me = flaky_get_me
    api.aclose = AsyncMock()
    _patch_telegram_api(monkeypatch, api)
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge._GETME_BACKOFF_INITIAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge._GETME_BACKOFF_MAX_SECONDS",
        0.01,
    )

    bridge = _new_bridge(tmp_path, monkeypatch, api_mock=api)
    # Stub the poll loop so the supervisor never touches state.json.
    poll_entered = asyncio.Event()

    async def fake_poll_loop() -> None:
        poll_entered.set()
        await asyncio.Event().wait()  # block forever (until cancel on stop())

    bridge._poll_loop = fake_poll_loop  # type: ignore[assignment]

    await bridge.start()
    try:
        await asyncio.wait_for(poll_entered.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            f"supervisor never reached poll loop; phase={bridge._bridge_phase!r}, "
            f"getMe calls={len(calls)}"
        )
    assert bridge._bridge_phase == "running"
    assert len(calls) == 2  # one failure, one success — no give-up

    await bridge.stop()


@pytest.mark.asyncio
async def test_stop_resets_stop_event_so_restart_works(tmp_path, monkeypatch) -> None:
    """The real Restart-is-broken cause: stop() set _stop_event and
    never cleared it, so the next start() saw is_set() == True and
    exited immediately. The fix replaces the event on stop()."""
    api = MagicMock()
    api.get_me = AsyncMock(return_value={"username": "test_bot"})
    api.aclose = AsyncMock()
    _patch_telegram_api(monkeypatch, api)

    bridge = _new_bridge(tmp_path, monkeypatch, api_mock=api)
    poll_entered = asyncio.Event()

    async def fake_poll_loop() -> None:
        poll_entered.set()
        await asyncio.Event().wait()

    bridge._poll_loop = fake_poll_loop  # type: ignore[assignment]

    await bridge.start()
    await asyncio.wait_for(poll_entered.wait(), timeout=2.0)
    assert bridge._bridge_phase == "running"

    await bridge.stop()
    assert bridge._bridge_phase == "stopped"
    assert not bridge._stop_event.is_set()

    # Second start cycle — supervisor spins up again, re-enters poll.
    poll_entered.clear()
    await bridge.start()
    await asyncio.wait_for(poll_entered.wait(), timeout=2.0)
    assert bridge._bridge_phase == "running"

    await bridge.stop()
