"""CR-1: ``ServerSession.kind`` discriminator (cockpit vs channel)."""

from __future__ import annotations

from unittest.mock import MagicMock

from tesseract.mirror.server.session import ServerSession


def test_cockpit_session_defaults_to_cockpit_kind() -> None:
    session = ServerSession(
        session_id="cockpit-1",
        ws=MagicMock(),
        chat_session=MagicMock(),
        event_log=MagicMock(),
    )
    assert session.kind == "cockpit"


def test_explicit_channel_kind_sticks() -> None:
    session = ServerSession(
        session_id="telegram-99",
        ws=MagicMock(),
        chat_session=MagicMock(),
        event_log=MagicMock(),
        kind="channel",
    )
    assert session.kind == "channel"


def test_bridge_built_session_is_channel(tmp_path, monkeypatch) -> None:
    """Telegram bridge's headless session constructor must stamp
    ``kind='channel'``."""
    from aiohttp import web

    from tesseract.integrations.telegram.bridge import TelegramBridge

    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._token = "fake"

    app = web.Application()
    # Minimal fields that _build_chat_session reads through.
    captured = {}

    def fake_build_chat_session(_app, session_id, *args, **kwargs):
        del _app, args, kwargs
        captured["session_id"] = session_id
        return MagicMock()

    monkeypatch.setattr(
        "tesseract.integrations.telegram.bridge._build_chat_session",
        fake_build_chat_session,
    )

    bridge._app = app
    session = bridge._build_headless_session(99)
    assert session.kind == "channel"
    assert session.session_id.startswith("telegram_99_")
