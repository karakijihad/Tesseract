"""Audit fix m2 — deterministic command router tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tesseract.integrations.telegram.commands import (
    TelegramCommandContext,
    dispatch,
    is_known_command,
)


def _ctx(*, app=None, tier="operator", offline=False, chat_id=1) -> TelegramCommandContext:
    return TelegramCommandContext(
        app=app or MagicMock(),
        chat_id=chat_id,
        tier=tier,
        offline=offline,
        bridge=MagicMock(_sessions={}),
    )


def test_is_known_command_recognises_canonical_set() -> None:
    for cmd in ("/help", "/status", "/queue", "/brief"):
        assert is_known_command(cmd), cmd
    assert is_known_command("/status@TarsBot")  # @bot suffix accepted
    assert not is_known_command("/unknown")
    assert not is_known_command("not a command")


@pytest.mark.asyncio
async def test_help_lists_commands() -> None:
    reply = await dispatch("/help", _ctx())
    assert reply is not None
    assert "/status" in reply
    assert "/queue" in reply


@pytest.mark.asyncio
async def test_status_online() -> None:
    reply = await dispatch("/status", _ctx(offline=False))
    assert reply == "TARS status: online"


@pytest.mark.asyncio
async def test_status_offline() -> None:
    reply = await dispatch("/status", _ctx(offline=True))
    assert reply == "TARS status: offline"


@pytest.mark.asyncio
async def test_friend_tier_blocked_from_operator_commands() -> None:
    reply = await dispatch("/queue", _ctx(tier="friend"))
    assert reply is not None
    assert "operator-only" in reply.lower()


@pytest.mark.asyncio
async def test_friend_tier_allowed_status_and_help() -> None:
    assert (await dispatch("/status", _ctx(tier="friend"))) == "TARS status: online"
    help_reply = await dispatch("/help", _ctx(tier="friend"))
    assert help_reply is not None
    assert "operator-only" in help_reply.lower()


@pytest.mark.asyncio
async def test_unknown_command_returns_none_for_fallthrough() -> None:
    reply = await dispatch("/nothing", _ctx())
    assert reply is None


@pytest.mark.asyncio
async def test_command_reply_routes_through_send_text_for_html_rendering(
    tmp_path, monkeypatch
) -> None:
    """Regression — /brief emits HTML (``<b>...</b>``).

    The bug they're guarding against: routing through ``_safe_send``
    omits ``parse_mode="HTML"``, so Telegram displays the raw ``<b>``
    tags. The fix routes command replies through ``send_text``, which
    uses ``_send_fresh(html=..., plain=...)`` — HTML-first, plain
    fallback only on parse error.
    """
    from unittest.mock import AsyncMock

    from tesseract.integrations._retention import RetentionPolicy
    from tesseract.integrations.telegram.api import TelegramMessage
    from tesseract.integrations.telegram.bridge import TelegramBridge
    from tesseract.integrations.telegram.state import StateBundle, save_allowlist

    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 1})
    bridge._api.send_chat_action = AsyncMock()
    bridge._conversations = MagicMock()
    bridge._app = MagicMock()
    bridge._app.get = lambda k, default=None: None
    bridge._sessions = {}
    bridge._retention = RetentionPolicy.fallback()
    bridge._poll_task = None
    bridge._stop_event = MagicMock()
    bridge._started_at = ""
    bridge._last_poll_at = None
    bridge._error_count = 0
    state_dir = tmp_path / "telegram"
    state_dir.mkdir(parents=True, exist_ok=True)
    bridge._state = StateBundle(dir_path=state_dir)
    bridge._state.allowlist.chat_ids.add(777)
    save_allowlist(bridge._state.allowlist_path, bridge._state.allowlist)

    msg = TelegramMessage(
        update_id=1,
        message_id=1,
        chat_id=777,
        chat_type="private",
        from_user_id=1,
        from_username="x",
        text="/help",
        date=0,
    )
    await bridge._handle_message(msg)

    # send_message must have been called with parse_mode="HTML" — the
    # bug was passing the raw text without parse_mode, which Telegram
    # then rendered as literal ``<b>`` tags.
    assert bridge._api.send_message.called
    call = bridge._api.send_message.call_args
    assert call.kwargs.get("parse_mode") == "HTML", (
        f"command reply did not use HTML parse mode; kwargs={call.kwargs}"
    )
