"""Multi-user substrate guards on the Telegram bridge.

Operator-only today; these tests pin the behaviour that keeps the future
multi-user expansion safe: groups can't trick the bridge into approving a
whole room via one chat_id. Post MO-9-10 the bridge writes inbound
messages to the per-channel conversation store rather than
``workspace_events``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.api import TelegramMessage
from tesseract.integrations.telegram.bridge import TelegramBridge


def _build_bridge(tmp_path, monkeypatch) -> TelegramBridge:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._api = MagicMock()
    bridge._api.send_chat_action = AsyncMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 1})
    bridge._conversations = MagicMock()
    bridge._app = MagicMock()
    bridge._sessions = {}
    bridge._retention = RetentionPolicy.fallback()
    bridge._poll_task = None
    bridge._stop_event = MagicMock()
    bridge._started_at = ""
    bridge._last_poll_at = None
    bridge._error_count = 0
    from tesseract.integrations.telegram.state import StateBundle

    state_dir = tmp_path / "telegram"
    state_dir.mkdir(parents=True, exist_ok=True)
    bridge._state = StateBundle(dir_path=state_dir)
    return bridge


@pytest.mark.asyncio
async def test_handle_message_rejects_group_chat(tmp_path, monkeypatch) -> None:
    """Non-private chat_type must short-circuit before any allowlist or
    pending-record mutation: groups share a single chat_id across every
    member, so approving a group = approving everyone in it. The bridge
    drops the message silently."""
    bridge = _build_bridge(tmp_path, monkeypatch)
    msg = TelegramMessage(
        update_id=1,
        message_id=1,
        chat_id=-100123,
        chat_type="supergroup",
        from_user_id=42,
        from_username="someone",
        text="hi",
        date=0,
    )

    await bridge._handle_message(msg)

    bridge._conversations.append.assert_not_called()
    bridge._api.send_message.assert_not_called()
    assert msg.chat_id not in bridge._state.allowlist.pending
    assert msg.chat_id not in bridge._state.allowlist.chat_ids


@pytest.mark.asyncio
async def test_handle_message_rejects_channel_chat(tmp_path, monkeypatch) -> None:
    bridge = _build_bridge(tmp_path, monkeypatch)
    msg = TelegramMessage(
        update_id=1,
        message_id=1,
        chat_id=-100456,
        chat_type="channel",
        from_user_id=None,
        from_username=None,
        text="broadcast",
        date=0,
    )

    await bridge._handle_message(msg)

    bridge._conversations.append.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_drops_blocked_chat(tmp_path, monkeypatch) -> None:
    """A chat_id on the blocked list short-circuits before any state
    mutation, conversation-store write, or API call. Matches the
    `/ignore` semantics — the operator decided this chat no longer
    exists; the bridge stays silent on every subsequent message."""
    bridge = _build_bridge(tmp_path, monkeypatch)
    bridge._state.allowlist.blocked.add(99999)
    from tesseract.integrations.telegram.state import save_allowlist

    save_allowlist(bridge._state.allowlist_path, bridge._state.allowlist)
    msg = TelegramMessage(
        update_id=1,
        message_id=1,
        chat_id=99999,
        chat_type="private",
        from_user_id=99999,
        from_username="blocked-user",
        text="ping",
        date=0,
    )

    await bridge._handle_message(msg)

    bridge._conversations.append.assert_not_called()
    bridge._api.send_message.assert_not_called()
    assert 99999 not in bridge._state.allowlist.pending
    assert 99999 not in bridge._state.allowlist.chat_ids
