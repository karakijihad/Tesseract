"""CR-5 — same tool on a cockpit session still hits the real ``ask_fn``
(no regression on the Mirror approval flow). The channel gate must only
install itself on channel-kind sessions.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tesseract.mirror.server.session import ServerSession


def test_cockpit_session_does_not_carry_channel_gate_state():
    """Cockpit sessions get no per-turn dedup set + no approval store —
    those are attached only by the channel bridge on channel sessions.
    """
    cockpit = ServerSession(
        session_id="cockpit-1",
        ws=MagicMock(),
        chat_session=MagicMock(),
        event_log=MagicMock(),
    )
    assert cockpit.kind == "cockpit"
    # Channel-gate attributes are absent on cockpit sessions (they are
    # attached lazily by ``build_channel_ask_fn`` only on the channel
    # path).
    assert not hasattr(cockpit, "_channel_gate_per_turn_emitted")
    assert not hasattr(cockpit, "_channel_gate_pending_approvals")


def test_channel_kind_explicit_on_telegram_bridge_session(monkeypatch, tmp_path):
    """The bridge stamps ``kind='channel'`` so the rest of the system
    can route on it. The channel-gate ask_fn install path is gated on
    that discriminator (cockpit path → ``app['prompt_builder']`` keeps
    the operator-attended approval card)."""
    from aiohttp import web
    from tesseract.integrations.telegram.bridge import TelegramBridge

    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._token = "fake"
    app = web.Application()
    bridge._app = app

    def _fake_build(_app, session_id, *args, **kwargs):
        del _app, args, kwargs
        cs = MagicMock()
        cs.session_id = session_id
        return cs

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge._build_chat_session",
        _fake_build,
    )
    session = bridge._build_headless_session(99)
    assert session.kind == "channel"
