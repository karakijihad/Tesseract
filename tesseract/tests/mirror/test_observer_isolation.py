"""mirror-multi-chat P4 — observer isolation across chats.

The observer subscriber is a singleton (one operator, one observer). It must
follow ``active_chat_id``: switching chats re-wires it to the new active chat
and clears the chat we're leaving so a background turn on that chat can't bleed
an observation into the chat we're now in. Governance rule #7 keeps the observer
session-global (one ``useObservationsStore``) — it fires on the active chat only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tesseract.brain.observer_subscriber import ObserverSubscriber
from tesseract.mirror.server import ws as ws_mod
from tesseract.mirror.server.session import ServerSession


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


class _FakeChat:
    """Mirrors ChatSession's observer attach/detach contract (chat.py:873-878)."""

    def __init__(self, history: list | None = None) -> None:
        self.history = list(history or [])
        self._observer_subscriber: object | None = None
        self._observer_last_index = 0

    def attach_observer_subscriber(self, sub: object) -> None:
        self._observer_subscriber = sub
        self._observer_last_index = len(self.history)

    def detach_observer_subscriber(self) -> None:
        self._observer_subscriber = None


def _session() -> ServerSession:
    class _WS:
        closed = False

        async def send_json(self, env: dict) -> None:
            pass

    return ServerSession(
        session_id="abcabcabcabcabcabcabcabcabcabc12",
        ws=_WS(),
        chat_session=_FakeChat(history=[{"role": "user", "content": "seed"}]),
        event_log=SimpleNamespace(append=lambda _e: None),
    )


def _arm_on_active(session: ServerSession) -> ObserverSubscriber:
    """Simulate the connect-time attach: subscriber bound to the active chat."""
    sub = ObserverSubscriber(observer=SimpleNamespace())
    active = session.chat_session
    active.attach_observer_subscriber(sub)
    sub.attach(active, lambda _s: None)
    return sub


def _armed_app(session: ServerSession, subscriber: ObserverSubscriber) -> dict:
    return {
        "observer_state": "armed",
        "observer_subscriber": subscriber,
        "server_sessions": {session.session_id: session},
    }


async def _settle() -> None:
    """The re-attach is spawned as a background task (kept off the switch critical
    path); let it run before asserting the new active chat is wired."""
    for _ in range(3):
        await asyncio.sleep(0)


async def test_switch_rewires_subscriber_to_new_active_chat() -> None:
    s = _session()
    a = s.chat_session
    sub = _arm_on_active(s)
    app = _armed_app(s, sub)
    b_id = s.create_chat(_FakeChat(history=[{"role": "user", "content": "b"}]))
    b = s.chats[b_id]

    await ws_mod._handle_chat_switch(app, s, {"chat_id": b_id})
    assert a._observer_subscriber is None        # outgoing cleared synchronously → no bleed
    await _settle()                              # background re-attach

    assert s.chat_session is b
    assert b._observer_subscriber is sub        # new active chat is wired
    assert sub._chat_session is b


async def test_switch_when_observer_off_is_noop() -> None:
    s = _session()
    b_id = s.create_chat(_FakeChat())
    app = {"observer_state": "off", "observer_subscriber": None, "server_sessions": {}}
    await ws_mod._handle_chat_switch(app, s, {"chat_id": b_id})
    assert s.active_chat_id == b_id


async def test_archive_active_rewires_subscriber_to_new_active() -> None:
    s = _session()
    a = s.chat_session
    a_id = s.active_chat_id
    sub = _arm_on_active(s)
    app = _armed_app(s, sub)
    b_id = s.create_chat(_FakeChat())
    b = s.chats[b_id]

    await ws_mod._handle_chat_archive(app, s, {"chat_id": a_id})
    assert a._observer_subscriber is None        # outgoing cleared synchronously
    await _settle()                              # background re-attach

    assert s.chat_session is b
    assert b._observer_subscriber is sub


async def test_archive_non_active_leaves_subscriber_on_active() -> None:
    s = _session()
    a = s.chat_session
    sub = _arm_on_active(s)
    app = _armed_app(s, sub)
    b_id = s.create_chat(_FakeChat())

    await ws_mod._handle_chat_archive(app, s, {"chat_id": b_id})

    assert s.chat_session is a                    # active unchanged
    assert a._observer_subscriber is sub
    assert sub._chat_session is a
