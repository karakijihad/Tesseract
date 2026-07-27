"""2026-05-17 — Telegram inbound parallelism + per-chat serialization.

Before this fix, ``_poll_loop`` did ``await self._handle_message(message)``
inline, so a 30s ``delegate_claude`` on chat A froze every other chat's
inbound for the duration. Operator (after the Claude timeout on a chat,
turn 54): *"telegram should also work in parallel, when
delegating to agents etc. similar to chat turns. no thread blocks."*

The fix wraps ``_handle_message`` in ``_handle_message_guarded``, spawned
as an ``asyncio.Task`` per inbound by ``_spawn_handler``. Inside the
guard, a per-chat ``asyncio.Lock`` (``_chat_locks[chat_id]``) keeps
two rapid inbounds on the *same* chat serial — otherwise history,
attachment decode, and outbound ordering would race.

These tests pin the contract directly against the bridge:
- Two inbounds on chat A and one on chat B run with A2 starting only
  after A1 completes, while B1 runs in parallel with A1/A2.
- ``stop()`` drains pending handlers within the 10s cap.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.bridge import TelegramBridge


def _new_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TelegramBridge:
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
    bridge._chat_locks = {}
    bridge._inflight_handlers = set()
    bridge._api = None
    bridge._chat_memory = None
    return bridge


@pytest.mark.asyncio
async def test_same_chat_serializes_via_lock(tmp_path, monkeypatch):
    """Two inbounds on the same chat run strictly in series."""
    bridge = _new_bridge(tmp_path, monkeypatch)
    started: list[tuple[int, float]] = []
    finished: list[tuple[int, float]] = []
    msg_order = []

    async def fake_handle(message):
        msg_order.append(message.message_id)
        started.append((message.message_id, time.monotonic()))
        await asyncio.sleep(0.1)
        finished.append((message.message_id, time.monotonic()))

    bridge._handle_message = fake_handle  # type: ignore[method-assign]

    msg_a1 = MagicMock(chat_id=100, message_id=1)
    msg_a2 = MagicMock(chat_id=100, message_id=2)
    bridge._spawn_handler(msg_a1)
    bridge._spawn_handler(msg_a2)

    # Drain
    await asyncio.gather(*list(bridge._inflight_handlers), return_exceptions=True)

    assert msg_order == [1, 2]
    # Second message must start AFTER first finishes (lock held).
    assert started[1][1] >= finished[0][1] - 0.005


@pytest.mark.asyncio
async def test_different_chats_run_in_parallel(tmp_path, monkeypatch):
    """A1 and B1 overlap; A1 does NOT block B1."""
    bridge = _new_bridge(tmp_path, monkeypatch)
    started_at: dict[int, float] = {}
    finished_at: dict[int, float] = {}

    async def fake_handle(message):
        started_at[message.message_id] = time.monotonic()
        await asyncio.sleep(0.1)
        finished_at[message.message_id] = time.monotonic()

    bridge._handle_message = fake_handle  # type: ignore[method-assign]

    bridge._spawn_handler(MagicMock(chat_id=100, message_id=1))
    bridge._spawn_handler(MagicMock(chat_id=200, message_id=2))
    await asyncio.gather(*list(bridge._inflight_handlers), return_exceptions=True)

    # Both started before either finished — parallel execution.
    assert started_at[2] < finished_at[1]
    assert started_at[1] < finished_at[2]


@pytest.mark.asyncio
async def test_handler_exception_does_not_kill_bridge(tmp_path, monkeypatch):
    """A crash in _handle_message must not leak past the guard."""
    bridge = _new_bridge(tmp_path, monkeypatch)

    async def crash(message):
        raise RuntimeError("simulated brain crash")

    bridge._handle_message = crash  # type: ignore[method-assign]
    bridge._spawn_handler(MagicMock(chat_id=100, message_id=1))
    await asyncio.gather(*list(bridge._inflight_handlers), return_exceptions=True)

    # Bridge is intact for the next inbound.
    handled: list[int] = []

    async def ok(message):
        handled.append(message.message_id)

    bridge._handle_message = ok  # type: ignore[method-assign]
    bridge._spawn_handler(MagicMock(chat_id=100, message_id=2))
    await asyncio.gather(*list(bridge._inflight_handlers), return_exceptions=True)
    assert handled == [2]


@pytest.mark.asyncio
async def test_stop_drains_pending_handlers(tmp_path, monkeypatch):
    """stop() waits for in-flight handlers (up to the 10s cap)."""
    bridge = _new_bridge(tmp_path, monkeypatch)
    finished: list[int] = []

    async def fake_handle(message):
        await asyncio.sleep(0.05)
        finished.append(message.message_id)

    bridge._handle_message = fake_handle  # type: ignore[method-assign]
    bridge._spawn_handler(MagicMock(chat_id=100, message_id=1))
    bridge._spawn_handler(MagicMock(chat_id=200, message_id=2))

    await bridge.stop()
    assert sorted(finished) == [1, 2]
    assert bridge._inflight_handlers == set()
