"""P6 Task 2 §G3 — Telegram (headless channel) idle-wake parity.

Design: Docs/Plan/lean-agent-os/idle-wake-design.md §G3. The gap was narrow:
`spawn_wake.wire_chat` was never called for bridge-built sessions, and the
wake delivery leg (`_run_chat_turn`) is cockpit-shaped. Remedy: wire at
`_build_headless_session` with a channel-shaped turn driver
(`TelegramBridge._wake_turn_driver`) that routes the wake turn through
`_start_channel_turn` and delivers via the bridge's real send path
(placeholder edit / fresh send) — the same pattern as
`_run_clear_reflection`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from tesseract.mirror.server import spawn_wake


def _fake_build_chat_session(_app, session_id, *args, **kwargs):
    del _app, args, kwargs
    cs = MagicMock()
    cs.session_id = session_id
    return cs


# --- wiring: _build_headless_session registers spawn_wake -----------------


def test_build_headless_session_wires_spawn_wake_with_channel_driver(
    monkeypatch, tmp_path,
) -> None:
    from tesseract.integrations.telegram.bridge import TelegramBridge

    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._token = "fake"
    app = web.Application()
    bridge._app = app

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge._build_chat_session",
        _fake_build_chat_session,
    )

    wired: list[tuple] = []

    def _fake_wire_chat(app_, session_, chat_id_, cs_, *, turn_driver=None):
        wired.append((app_, session_, chat_id_, cs_, turn_driver))

    monkeypatch.setattr(spawn_wake, "wire_chat", _fake_wire_chat)

    session = bridge._build_headless_session(99)

    assert len(wired) == 1
    wired_app, wired_session, wired_chat_id, wired_cs, wired_driver = wired[0]
    assert wired_app is app
    assert wired_session is session
    assert wired_chat_id == session.active_chat_id
    assert wired_driver == bridge._wake_turn_driver


# --- delivery leg: _wake_turn_driver routes through the bridge send path --


class _ProgressStub:
    def __init__(self) -> None:
        self._throttler = SimpleNamespace(stop=AsyncMock())

    async def __call__(self, event) -> None:
        pass


def _make_bridge_stub():
    return SimpleNamespace(
        name="telegram",
        _send_thinking_placeholder=AsyncMock(return_value=111),
        _build_progress_callback=MagicMock(
            side_effect=lambda chat_id, placeholder_id: _ProgressStub(),
        ),
        _send_outbound=AsyncMock(),
        _edit_or_fallback=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_wake_turn_driver_delivers_reply_via_send_outbound(monkeypatch) -> None:
    from tesseract.integrations.telegram.bridge import TelegramBridge

    async def _fake_start_channel_turn(app, session, *, channel, chat_id, body, on_progress=None, **kwargs):
        assert body == spawn_wake._WAKE_NUDGE
        assert chat_id == "42"
        return "a background task finished — here's what happened"

    monkeypatch.setattr(
        "tesseract.mirror.server.ws._start_channel_turn", _fake_start_channel_turn,
    )

    bridge = _make_bridge_stub()
    session = SimpleNamespace(channel_chat_id="42", active_chat_id="internal-chat-id")

    await TelegramBridge._wake_turn_driver(bridge, app=MagicMock(), session=session, chat_id="internal-chat-id")

    bridge._send_outbound.assert_awaited_once_with(
        42, "a background task finished — here's what happened", placeholder_id=111,
    )
    bridge._edit_or_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_turn_driver_clears_self_registration_before_channel_turn(
    monkeypatch,
) -> None:
    """Regression: ``schedule_wake`` pre-registers its own wrapper task into
    ``current_turn_tasks[active_chat_id]`` (needed so ``chat_idle()`` reports
    busy while the wake runs). Bridge sessions have exactly one chat, so
    that is the SAME slot ``_start_channel_turn``'s busy-check reads via
    ``session.current_turn_task``. Without clearing it first, the
    busy-check would find the currently-executing wrapper task (not done —
    we're inside it) and await itself, which asyncio rejects as a
    self-await (silently swallowed by ``_start_channel_turn``'s broad
    ``except Exception: pass``, but wrong). Pins that the driver clears its
    own registration before handing off."""
    from tesseract.integrations.telegram.bridge import TelegramBridge
    from tesseract.mirror.server.session import ServerSession

    seen_registration: dict[str, object] = {}

    _ABSENT = object()

    async def _fake_start_channel_turn(app, session, *, channel, chat_id, body, on_progress=None, **kwargs):
        # `current_turn_task`'s setter pops the key entirely on `= None`
        # (session.py:378-379) rather than storing a `None` value, so
        # "cleared" means the key is gone, not present-with-None.
        seen_registration["value"] = session.current_turn_tasks.get(
            session.active_chat_id, _ABSENT,
        )
        return "wake reply"

    monkeypatch.setattr(
        "tesseract.mirror.server.ws._start_channel_turn", _fake_start_channel_turn,
    )

    bridge = _make_bridge_stub()
    # Real ServerSession (not a SimpleNamespace) so `current_turn_task = None`
    # exercises the actual property setter that pops `current_turn_tasks`
    # (session.py:375-381) — the mechanism under test.
    session = ServerSession(
        session_id="telegram_42_test",
        ws=MagicMock(),
        chat_session=MagicMock(),
        event_log=MagicMock(append=MagicMock()),
        kind="channel",
    )
    session.channel_chat_id = "42"
    # Simulates schedule_wake's pre-registration of the wrapper task under
    # the same slot _start_channel_turn's busy-check reads.
    session.current_turn_tasks[session.active_chat_id] = object()

    await TelegramBridge._wake_turn_driver(
        bridge, app=MagicMock(), session=session, chat_id=session.active_chat_id,
    )

    assert seen_registration["value"] is _ABSENT


@pytest.mark.asyncio
async def test_wake_turn_driver_falls_back_on_empty_reply(monkeypatch) -> None:
    from tesseract.integrations.telegram.bridge import TelegramBridge

    async def _fake_start_channel_turn(app, session, *, channel, chat_id, body, on_progress=None, **kwargs):
        return None

    monkeypatch.setattr(
        "tesseract.mirror.server.ws._start_channel_turn", _fake_start_channel_turn,
    )

    bridge = _make_bridge_stub()
    session = SimpleNamespace(channel_chat_id="42", active_chat_id="internal-chat-id")

    await TelegramBridge._wake_turn_driver(bridge, app=MagicMock(), session=session, chat_id="internal-chat-id")

    bridge._send_outbound.assert_not_awaited()
    bridge._edit_or_fallback.assert_awaited_once()


# --- fix pass 1: wake driver resets per-turn channel-gate dedup state -----


@pytest.mark.asyncio
async def test_wake_turn_driver_resets_per_turn_gate_state(monkeypatch) -> None:
    """Regression: the two existing channel turn call sites (inbound message
    handling at bridge.py:689, ``_run_clear_reflection`` at bridge.py:2234)
    both call ``reset_per_turn_state(session)`` at turn entry so a tool+args
    hash gated in a prior turn can re-emit a fresh ``tars_post`` nudge.
    ``_wake_turn_driver`` is a third caller of the channel turn path but was
    missing this call, so the per-turn dedup set from a PRIOR turn silently
    suppressed the ASK gate during wake turns. Pins that the driver clears
    it too."""
    from tesseract.integrations._channel_gate import _PER_TURN_ATTR
    from tesseract.integrations.telegram.bridge import TelegramBridge

    async def _fake_start_channel_turn(app, session, *, channel, chat_id, body, on_progress=None, **kwargs):
        return "wake reply"

    monkeypatch.setattr(
        "tesseract.mirror.server.ws._start_channel_turn", _fake_start_channel_turn,
    )

    bridge = _make_bridge_stub()
    session = SimpleNamespace(channel_chat_id="42", active_chat_id="internal-chat-id")
    setattr(session, _PER_TURN_ATTR, {"stale-hash-from-a-prior-gated-turn"})

    await TelegramBridge._wake_turn_driver(
        bridge, app=MagicMock(), session=session, chat_id="internal-chat-id",
    )

    assert getattr(session, _PER_TURN_ATTR) == set()


# --- idle vs busy: channel wake honours the same dedup/idle gate ----------


class _Task:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _Handle:
    handle_id = "del-claude-1"
    kind = "delegate_claude"

    def status(self) -> str:
        return "done"


def test_idle_headless_session_schedules_channel_wake_turn(monkeypatch) -> None:
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        spawn_wake, "schedule_wake",
        lambda app, session, chat_id, turn_driver=None: scheduled.append(
            (chat_id, turn_driver),
        ),
    )

    driver = object()  # stands in for bridge._wake_turn_driver
    session = SimpleNamespace(
        current_turn_tasks={}, spawn_wake_pending=set(), chats={},
    )
    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="A", handle=_Handle(),
        floor=lambda h: None, turn_driver=driver,
    )
    assert scheduled == [("A", driver)]


def test_busy_headless_session_floor_only_no_channel_wake(monkeypatch) -> None:
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        spawn_wake, "schedule_wake",
        lambda app, session, chat_id, turn_driver=None: scheduled.append(
            (chat_id, turn_driver),
        ),
    )

    driver = object()
    session = SimpleNamespace(
        current_turn_tasks={"A": _Task(done=False)},
        spawn_wake_pending=set(),
        chats={},
    )
    ingested: list = []
    spawn_wake.on_spawn_complete(
        None, session, cs=object(), chat_id="A", handle=_Handle(),
        floor=ingested.append, turn_driver=driver,
    )
    assert len(ingested) == 1
    assert scheduled == []
